"""睿云智能工作台 UI 自动化驱动（Electron + CDP）。

设计要点：
  * 用 DOM 级定位而非屏幕坐标 → 窗口大小/位置变化不影响稳定性
  * 发送前记录日志目录快照，发送后等待新会话落盘并稳定
  * 提供 discover() 诊断模式，用于探查真实选择器
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
import sys
from typing import Optional

from drivers.cdp import (
    CDPSession, CDPError, list_targets, pick_page_target, wait_for_port,
)

# 永不自动点击的选项文本（点了任务就终止，违背「完整跑完」目标）
DENY_WORDS = ("拒绝", "取消", "关闭", "停止", "终止")


# ------------------------------------------------------------------ 控制台文案
# 控制台是给用户看的，不是模块内部日志：前缀统一成 [应用] / [确认]，
# 不再出现 [ui_driver]、[auto-confirm] 这类模块名与 mode / cls 之类的内部字段。

# 自动确认动作 → 大白话（与 core/trajectory.CONFIRM_MODE_LABEL 同口径）
CONFIRM_ACTION_CN = {
    "qcard-option": "选中选项",
    "qcard-confirm": "确认本题",
    "qcard-submit": "提交",
    "qcard-fill": "填写自定义答案",
    "keyword": "确认授权",
    "first-option": "确认首个选项",
}


def cite_line(mode: str, q: str = "", question: str = "", answer: str = "",
              extra: str = "") -> str:
    """拼一条「确认」日志：题号与题干只说一次，答案跟在后面。

    形态：[确认] 12:03:41 第 1/4 题 · 选中选项 · 选项文字 —— 题干……
    题干缺失（旧数据 / 未能读到标题）时退化为只报题号，不显示空占位。
    """
    label = CONFIRM_ACTION_CN.get(str(mode or ""), str(mode or "自动确认"))
    head = f"第 {q} 题" if q else label
    body = " · ".join(x for x in (label if q else "", answer) if x)
    if question:
        body = f"{body} —— {question}" if body else question
    if extra:
        body = f"{body}（{extra}）" if body else f"（{extra}）"
    return f"[确认] {time.strftime('%H:%M:%S')} {head}" + (f" {body}" if body else "")


def cite_emit(text: str) -> None:
    print(text, flush=True)


# 输入框候选选择器（按优先级）
INPUT_SELECTORS = [
    "textarea",
    'div[contenteditable="true"]',
    '[contenteditable="true"]',
    'input[type="text"]',
    '[role="textbox"]',
]

# 发送按钮候选（文本 / aria-label 关键词）
SEND_KEYWORDS = ["发送", "send", "submit"]

# 附件入口（实测 DOM）：按钮 title 形如「引用本地文件 (1/10)」，
# 「(n/10)」即当前引用数与上限，是判断投递是否成功最可靠的外部信号。
ATTACH_BUTTON_SELECTOR = "button.chat-input-btn--attach"
# 输入区容器（拖拽落点），实测结构 chat-input-shell > … > textarea.chat-input-textarea
COMPOSER_SELECTOR = "div.chat-input-shell"

# 界面就绪判定：应用启动会先显示一个 data: 授权外壳页，需等到主 UI（本地 http 服务）挂载完成。
# 实测（Windows 2.0.14）：主界面跑在 http://127.0.0.1:5173/，端口后于 CDP 端口就绪，
# 且标题会先短暂显示 "127.0.0.1:5173" 再变成「睿云智能工作台」，所以只认 http+正文长度。
READY_JS = r"""
(() => {
  const href = location.href || '';
  const h = href.toLowerCase();
  const bodyLen = document.body ? document.body.innerText.length : 0;
  const isHttp = h.startsWith('http://127.0.0.1') || h.startsWith('http://localhost')
              || h.startsWith('http://[::1]');
  return { href: href.slice(0, 120), bodyLen: bodyLen, ready: isHttp && bodyLen > 200 };
})()
"""

DISCOVER_JS = r"""
(() => {
  const info = (el) => {
    const attrs = {};
    for (const a of (el.attributes || [])) attrs[a.name] = a.value;
    const r = el.getBoundingClientRect();
    const vis = r.width > 0 && r.height > 0;
    return {
      tag: el.tagName.toLowerCase(),
      attrs: attrs,
      cls: (el.className || '').toString().slice(0, 120),
      placeholder: el.getAttribute('placeholder'),
      ariaLabel: el.getAttribute('aria-label'),
      text: (el.innerText || el.value || '').trim().slice(0, 30),
      visible: vis,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]
    };
  };
  const inputs = [...document.querySelectorAll(
    'textarea,input,[contenteditable="true"],[role="textbox"]'
  )].map(info);
  const btns = [...document.querySelectorAll('button,[role="button"],svg,[class*="send" i]')]
    .slice(0, 40).map(info);
  return {
    url: location.href,
    title: document.title,
    bodyText: document.body ? document.body.innerText.slice(0, 4000) : '',
    inputs, buttons: btns
  };
})()
"""


class RuiyunUIDriver:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        app = cfg["app"]
        self.binary = app["binary"]
        self.port = int(app["debug_port"])
        self.launch_timeout = float(app.get("launch_timeout_s", 60))
        self.session_root = Path(cfg["paths"]["session_root"])
        self.proc: Optional[subprocess.Popen] = None
        self.cdp: Optional[CDPSession] = None
        self.target_url: str = ""
        self.env_profile_desc: str = ""
        self._input_selector: Optional[str] = None
        self._launched_by_us = False
        # 本次是否复用了已在运行的应用（而非新启动），供上层日志区分
        self.reused_existing = False
        # 自动确认（CDP 点击确认卡片）：配置与事件留痕
        app_cfg = cfg.get("app", {}) or {}
        # 应用已在运行但没有调试端口时，是否先关掉再以调试模式重启。
        # 必须重启的原因见 launch()：Electron 单实例会把新进程的启动参数吞掉，
        # 不重启就永远拿不到可驱动的实例（环境档案也只在启动时注入）。
        # 若希望自己手动控制（例如应用里正跑着重要任务），置 false 走「关闭实例」按钮。
        self.restart_if_no_port = bool(app_cfg.get("restart_if_no_debug_port", True))
        # 「界面就绪」的等待预算：调试端口起来 ≠ 界面可用。实测 Windows dev 环境下
        # 应用可能先弹登录窗（ruiyun-dev.3ren.cn），主界面要等登录流程走完才渲染，
        # 60s 常常不够（表现为「端口上有页面，但正文为空、接不进去」）。
        self.ui_ready_timeout = float(app_cfg.get("ui_ready_timeout_s", 180))
        self.auto_confirm = bool(app_cfg.get("auto_confirm", False))
        self.confirm_keywords = list(app_cfg.get("confirm_keywords") or [])
        # 同一文本的卡片点击冷却秒数（给应用处理时间，也防刷点）
        self._confirm_cooldown = float(app_cfg.get("confirm_cooldown_s", 3))
        # 单轮累计点击上限（长任务可能需要大量授权，触发即转人工）
        self._confirm_max_clicks = int(app_cfg.get("confirm_max_clicks", 200))
        self.confirm_events: list = []      # [{time, ts, mode, text, cls, parentCls, q, sess}]
        self._clicked_groups: dict = {}     # 选项组签名 → {ts, attempts}
        self._confirm_cdp_streak = 0        # CDP 连续异常次数（重连后仍失败才计）
        self.confirm_human_needed = False   # 连续 5 次点击失败 → 请求人工介入
        # 转人工那一刻所在会话（绝对路径），供上游把 P0 标记挂到正确的用例
        self.confirm_human_sess = None
        # agent-question-composer 选项卡：DOM 事实驱动，内存只留「指纹 + 次数」
        # 实测（scripts/probe_qcard.py --switch-test）：切换会话视图会卸载卡片组件、
        # 清空已选答案，卡片退回第 1 题。所以卡片所在的会话视图必须锁定，
        # 否则多会话巡检每轮都在点第 1 题 —— 表现为「反复点、状态不推进直到熔断」。
        self._qcard_owner_sess = None       # 卡片所在会话目录（锁定标志）
        self._qcard_view_sess = None        # 当前视图对应的会话目录
        # 当前在途会话（绝对路径字符串）：流水线在会话目录出现后写入。
        # 点击事件带上它才能归到正确用例 —— 并发下这是唯一可靠的信息源，
        # 事后按时间窗推断必然串台（见 run_pipeline._collect）。
        self._current_sess = None
        self._qcard_fp_sig = ""             # 当前进度指纹
        self._qcard_fp_since = 0.0          # 当前指纹首次出现时间
        self._qcard_fp_clicks: dict = {}    # 指纹 → 累计尝试次数（卡片存续期内累计）
        self._qcard_card_clicks = 0         # 本张卡片累计「有效点击」次数
        self._qcard_card_since = 0.0        # 本张卡片首次出现时间
        self._qcard_last_click = 0.0        # 最近一次点击尝试时间（节流用）
        # 选项卡阈值（均可在 config.yaml 的 app 段覆盖，缺省即可运行）
        self._qcard_min_click_gap = float(app_cfg.get("qcard_min_click_interval_s", 0.4))
        self._qcard_same_limit = int(app_cfg.get("qcard_same_card_limit", 6))
        self._qcard_stall_s = float(app_cfg.get("qcard_stall_s", 30))
        self._qcard_card_clicks_limit = int(app_cfg.get("qcard_card_clicks_limit", 12))
        self._qcard_card_max_s = float(app_cfg.get("qcard_card_max_s", 120))
        self._qcard_fill_text = str(app_cfg.get("qcard_fill_text")
                                    or "请你按最合理的方式继续，不用再问我")
        # 多会话视图巡检：并发下确认卡片只渲染在当前可见会话，后台会话的卡
        # 需要依次切换视图才能看到。cycle_s=0 表示关闭巡检。
        self.auto_confirm_cycle_s = float(app_cfg.get("auto_confirm_cycle_s", 6))
        self.view_cycle_disabled = False    # 连续读不到会话列表 → 停用巡检（降级）
        self._cycle_fail_streak = 0         # 连续读不到列表的轮数
        self._cycle_visited: set = set()    # 本轮已扫过的列表项（同名会话轮转去重）

    # ------------------------------------------------------------ 生命周期
    def _apply_env_profile(self, env: dict) -> str:
        """按配置档案注入环境变量（默认 dev），实现启动即切环境。

        优先级：调用方显式 export 的变量 > 档案值 > 应用内置默认。
        用 setdefault 而非直接赋值 → 手动切换其他环境（如 launch_app_dev.sh、
        临时 export test/prod 变量）不会被覆盖。
        返回实际生效的档案名，供日志与验证使用。
        """
        profile = str(self.cfg.get("app", {}).get("env_profile") or "").strip()
        if not profile:
            return "(未指定环境，沿用当前进程已有的环境变量)"
        pairs = (self.cfg.get("app", {}).get("env_profiles") or {}).get(profile) or {}
        applied = 0
        for k, v in pairs.items():
            if k not in env:            # 显式设置过的变量不覆盖
                env[k] = str(v)
                applied += 1
        return f"{profile}（注入 {applied} 个变量，另 {len(pairs) - applied} 个已被现有环境占用）"

    def is_up(self) -> bool:
        return bool(list_targets(self.port))

    def launch(self, wait: bool = True) -> bool:
        """确保应用以调试模式运行 —— 已运行则复用，未运行才启动。

        平台永不主动关闭应用，因此这里的语义是「探测 + 按需唤醒」：
        调试端口活着就直接复用（不持有进程句柄），否则才新起一个进程。
        """
        if self.is_up():
            # 复用已有实例：不设置 proc / _launched_by_us，避免把它当成自己的子进程
            self.reused_existing = True
            return True
        args = [
            self.binary,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            # 受限环境下 GPU/沙箱进程无法初始化，Electron 会直接 FATAL 退出
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--no-sandbox",
        ]
        env = dict(os.environ)
        # 关键：父进程若带 ELECTRON_RUN_AS_NODE=1，Electron 会退化成纯 Node
        # 并拒绝 --remote-debugging-port（实测报 "bad option"），必须清除。
        env.pop("ELECTRON_RUN_AS_NODE", None)
        env.setdefault("ELECTRON_ENABLE_LOGGING", "0")
        profile_desc = self._apply_env_profile(env)   # 启动即切环境（默认 dev）
        self.env_profile_desc = profile_desc
        if self.cfg.get("app", {}).get("log_env_profile", True):
            print(f"[应用] 启动环境：{profile_desc}", flush=True)

        # 端口不通但应用进程还在 → 那是「没有调试端口」的实例（用户从开始菜单/自启动
        # 拉起的）。Electron 是单实例应用：此时直接 Popen 新进程，argv 会被转交给旧
        # 实例、新进程立刻退出，调试端口永远不会打开。实测（Windows）：
        #   应用已在运行 → launch() 起的新进程 code=0 秒退、进程数不增、端口始终不监听，
        #   22.5s 后只换来一句「应用启动失败或调试端口未就绪」。
        # 所以必须先关掉这些实例再以调试模式启动 —— 与 launch_app_dev.sh/.ps1 的
        # 「杀到干净再启动」同一策略；环境档案也只在启动时注入，本来就需要重启才生效。
        stale = self._running_app_pids()
        if stale:
            if not self.restart_if_no_port:
                print(f"[应用] 应用已在运行，但没有调试端口（pid {stale}），"
                      "而配置里禁止自动重启：请先在界面点「✕ 关闭实例」，再运行测试",
                      flush=True)
                return False
            print(f"[应用] 发现 {len(stale)} 个已运行的应用实例（缺少调试端口），"
                  "先关闭再以调试模式启动", flush=True)
            self._kill_running_apps()

        self.proc = self._spawn(args, env)
        self._launched_by_us = True
        if not wait:
            return True
        ok = wait_for_port(self.port, self.launch_timeout)
        if not ok and self.proc.poll() is not None and self.restart_if_no_port:
            # 竞态兜底：我们探测端口之后、启动之前，用户又拉起了一个无端口实例，
            # 于是这一发同样被单实例吞掉。关掉它再重试一次（只重试一次）。
            again = self._running_app_pids()
            if again:
                print(f"[应用] 新进程被已有实例接管并退出（code={self.proc.returncode}），"
                      f"关闭 {len(again)} 个已有实例后重试一次…", flush=True)
                self._kill_running_apps()
                self.proc = self._spawn(args, env)
                ok = wait_for_port(self.port, self.launch_timeout)
        if not ok:
            self._report_launch_failure()
        return ok

    def _spawn(self, args: list, env: dict):
        """拉起应用进程（Windows 用独立进程组 + DETACHED，其它平台新会话）。"""
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, **kwargs
        )

    def _running_app_pids(self) -> list:
        """在跑的应用进程 pid（按可执行文件名匹配，覆盖手动启动的实例）。

        为什么按**字节**解析 tasklist 输出：它的输出编码随控制台代码页而变
        （`chcp 65001` 下是 UTF-8，中文系统默认是 GBK）。若按 locale 解码再比对
        中文进程名，UTF-8 输出会被解成 `鐫夸簯鏅鸿兘...` 之类的乱码，
        进程名永远比不上、函数恒返回空 —— 「检测已有实例」这一步会静默失效
        （实测：修复前正是这个原因导致它查不到已在运行的应用）。
        进程名交给 tasklist 的 /FI 过滤器（内部按 Unicode 匹配），
        pid 是纯数字与编码无关，因此这里只认「以引号开头的 CSV 行」即可。
        """
        name = Path(self.binary).name
        if not name:
            return []
        if sys.platform != "win32":
            try:
                out = subprocess.run(["pgrep", "-x", name],
                                     capture_output=True, text=True, timeout=10).stdout
                return [int(x) for x in out.split() if x.strip().isdigit()]
            except Exception:
                return []
        try:
            raw = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15).stdout
        except Exception:
            return []
        pids = []
        for line in raw.split(b"\n"):
            s = line.strip()
            if not s.startswith(b'"'):
                continue          # 「没有运行的任务…」之类的提示行，不是进程行
            cols = s.split(b",")
            if len(cols) < 2:
                continue
            pid = cols[1].strip().strip(b'"')
            if pid.isdigit():
                pids.append(int(pid))
        return pids

    def _kill_running_apps(self) -> None:
        """关闭所有在跑的应用进程，并等到进程与端口都真正释放。

        **先优雅关闭再强杀**：`taskkill /F`（等价 SIGKILL）不给应用收尾机会，
        实测会丢掉 dev 登录态 —— 下次启动弹出登录窗（ruiyun-dev.3ren.cn），
        主界面要等登录流程走完才渲染，于是「调试端口起来了但 60s 内接不进界面」。
        不带 /F 的 taskkill 会发 WM_CLOSE，应用能正常落盘会话并退出。
        """
        name = Path(self.binary).name
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/IM", name],
                               capture_output=True, timeout=20)
            else:
                subprocess.run(["pkill", "-x", name], capture_output=True, timeout=20)
        except Exception as exc:
            print(f"[应用] 关闭已有实例失败：{type(exc).__name__}: {exc}", flush=True)
        for _ in range(20):                     # 最多等 10s 优雅退出
            if not self._running_app_pids() and not self.is_up():
                print("[应用] 已有实例已关闭，可以重新启动", flush=True)
                return
            time.sleep(0.5)
        graceful = False

        print("[应用] 关闭太慢，改用强制结束（可能丢掉应用的登录态）", flush=True)
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/IM", name],
                               capture_output=True, timeout=20)
            else:
                subprocess.run(["pkill", "-9", "-x", name],
                               capture_output=True, timeout=20)
        except Exception as exc:
            print(f"[应用] 强制结束失败：{type(exc).__name__}: {exc}", flush=True)
        for _ in range(20):
            if not self._running_app_pids() and not self.is_up():
                print("[应用] 已有实例已强制结束", flush=True)
                return
            time.sleep(0.5)
        print("[应用] 已有实例还没完全退出，仍继续尝试启动", flush=True)
        del graceful

    def _report_launch_failure(self) -> None:
        """调试端口没起来时，把可区分的根因写清楚（而不是一句笼统的失败）。"""
        if not Path(self.binary).is_file():
            hint = f"可执行文件不存在：{self.binary}"
        else:
            code = self.proc.poll() if self.proc else None
            alive = self._running_app_pids()
            if code is not None:
                hint = (f"新进程已退出（code={code}）：应用是单实例的，启动参数被交给了"
                        "已在运行的实例，所以调试端口不会打开")
            elif alive:
                hint = (f"新进程还在运行，但端口 {self.port} 没就绪；当前有 {len(alive)} 个"
                        "应用进程 —— 可能是安全软件拦截了调试端口，或该端口被别的程序占用")
            else:
                hint = "应用进程不存在：启动参数或可执行文件路径有问题"
        print(f"[应用] 等待 {self.launch_timeout:.0f}s，调试端口 {self.port} 仍未就绪 —— {hint}",
              flush=True)

    def attach(self, timeout_s: Optional[float] = None) -> bool:
        """连接主界面渲染进程，直到主 UI 真正挂载完成。

        应用启动过程中会出现多个 page 目标（data: 授权外壳页、about:blank、
        登录窗、主窗口），且主窗口晚于调试端口就绪。这里用 READY_JS 做就绪判定，
        只有本地 http 服务且正文已渲染的目标才算可用。

        默认等待预算是 ui_ready_timeout_s（180s），比 launch_timeout（端口就绪，
        60s）宽松：dev 环境启动会先走登录窗，主界面渲染明显晚于端口。
        """
        deadline = time.time() + (timeout_s or self.ui_ready_timeout)
        wait_s = timeout_s or self.ui_ready_timeout
        while time.time() < deadline:
            for tgt in list_targets(self.port):
                if tgt.get("type") != "page":
                    continue
                ws = tgt.get("webSocketDebuggerUrl")
                if not ws:
                    continue
                try:
                    cdp = CDPSession(ws)
                    cdp.enable_runtime()
                    state = cdp.eval_js(READY_JS, timeout=10) or {}
                    if state.get("ready"):
                        self.cdp = cdp
                        self.target_url = state.get("href", "")
                        return True
                    cdp.close()
                except Exception:
                    continue
            time.sleep(1)
        # 失败时把端口上的真实目标打出来：端口活着却没有可用主界面，可能是
        # ① 应用还停在外壳页/登录页 ② 端口被别的程序占用（端口号撞车）。
        # 这两种情况的处理方式完全不同，日志必须能区分。
        tgts = list_targets(self.port)
        print(f"[应用] 等待 {wait_s:.0f}s，仍没有可用的主界面"
              f"（调试端口上共 {len(tgts)} 个页面）", flush=True)
        for t in tgts[:8]:
            print("    {:<8s} {}  {}".format(
                str(t.get("type")), (t.get("url") or "")[:90],
                (t.get("title") or "")[:40]), flush=True)
        return False

    def ensure_ready(self) -> tuple:
        """唤醒并附着：应用未运行则启动，已运行则直接复用。

        返回 (ok, how)，how ∈ {"reused", "launched"}，供调用方打印区分。
        这是所有入口（pipeline / repro / discover）的统一接入点。
        """
        if not self.launch():
            return False, "launch_failed"
        how = "reused" if self.reused_existing else "launched"
        if not self.attach():
            return False, how
        return True, how

    def detach(self) -> None:
        """仅断开 CDP 连接，**不关闭应用**。

        平台策略：应用常驻。测试结束后应用保持运行，下次运行直接复用，
        因此这里刻意不杀进程。需要真正关闭应用时请显式调用 kill_app()，
        或使用 launch_app_dev.sh --stop。
        """
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:
                pass
            self.cdp = None

    def kill_app(self) -> None:
        """显式关闭由本驱动启动的应用进程（默认路径不会调用）。

        只对「我们自己启动的」实例生效（_launched_by_us）；复用的实例不归我们管。
        Windows 不支持 os.killpg，改用 proc.terminate()/proc.kill()。
        """
        if self.proc and self._launched_by_us:
            try:
                if sys.platform == "win32":
                    self.proc.terminate()
                else:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            self._launched_by_us = False

    # 兼容旧调用点：历史上 shutdown() 会关应用，现在只断开连接
    def shutdown(self) -> None:
        self.detach()

    # ------------------------------------------------------------ 诊断
    def discover(self) -> dict:
        return self.cdp.eval_js(DISCOVER_JS, timeout=20) or {}

    def _resolve_input(self, wait_s: float = 30.0) -> Optional[str]:
        """自动定位输入框并缓存。React 挂载需要时间，这里带等待重试。"""
        if self._input_selector:
            return self._input_selector
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for sel in INPUT_SELECTORS:
                expr = (
                    f"(() => {{ const els=[...document.querySelectorAll({json.dumps(sel)})];"
                    f" const v=els.find(e=>{{const r=e.getBoundingClientRect();"
                    f" return r.width>0&&r.height>0;}});"
                    f" return v ? {json.dumps(sel)} : null; }})()"
                )
                try:
                    if self.cdp.eval_js(expr, timeout=15):
                        self._input_selector = sel
                        return sel
                except CDPError:
                    pass
            time.sleep(1)
        return None

    def dump_dom_snapshot(self, name: str = "ui_dom_error.json") -> dict:
        """把当前页面状态与可交互元素导出到 artifacts/（选择器失效时的现场）。

        与 discover_ui.py 同源（都用 DISCOVER_JS），区别是**自动触发**：
        输入框定位不到、文本写不进去时，必须留下「页面当时到底长什么样」，
        否则跨平台改选择器只能靠猜。返回 {path, summary} 供日志与异常信息引用。
        """
        out = {"page": self.page_state()}
        try:
            out["dom"] = self.discover()
        except Exception as exc:
            out["dom_error"] = f"{type(exc).__name__}: {exc}"
        try:
            root = Path(__file__).resolve().parent.parent / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            p = root / name
            p.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            path = str(p)
        except OSError as exc:
            path = f"(导出失败: {exc})"
        st = out.get("page") or {}
        summary = (f"页面 {st.get('href') or '?'}"
                   f" | 登录页={st.get('login_like')}"
                   f" | 输入区={st.get('has_composer')}"
                   f" | 正文 {(st.get('body_head') or '')[:60]}")
        return {"path": path, "summary": summary}

    # ------------------------------------------------------------ 输入
    def _focus_input(self, sel: str) -> bool:
        expr = (
            f"(() => {{ const e=document.querySelector({json.dumps(sel)});"
            f" if(!e) return false; e.focus();"
            f" const r=e.getBoundingClientRect();"
            f" return r.width>0&&r.height>0; }})()"
        )
        try:
            return bool(self.cdp.eval_js(expr, timeout=15))
        except CDPError:
            return False

    def _element_center(self, sel: str) -> Optional[dict]:
        """元素中心坐标（不可见返回 None）。"""
        expr = (
            f"(() => {{ const e=document.querySelector({json.dumps(sel)});"
            f" if(!e) return null; const r=e.getBoundingClientRect();"
            f" if(r.width<=0||r.height<=0) return null;"
            f" return {{x: Math.round(r.x+r.width/2),"
            f"          y: Math.round(r.y+r.height/2)}}; }})()"
        )
        try:
            pt = self.cdp.eval_js(expr, timeout=10)
        except CDPError:
            return None
        return pt if isinstance(pt, dict) else None

    def click_at_point(self, pt: dict) -> None:
        """按坐标发真实鼠标左键点击（mousePressed + mouseReleased）。

        与 JS 的 `el.click()` / `el.focus()` 的关键差别：真实鼠标事件产生
        **user activation**，并走完整的命中测试与焦点链路 —— 受控输入组件据此
        才会进入「可输入 / 可发送」状态（新版构建上尤其如此）。
        """
        for etype, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.cdp.call("Input.dispatchMouseEvent", {
                "type": etype, "x": int(pt["x"]), "y": int(pt["y"]),
                "button": "left", "buttons": buttons, "clickCount": 1,
            }, timeout=10)

    def _click_selector(self, sel: str) -> bool:
        """真实点击某选择器命中的元素。"""
        pt = self._element_center(sel)
        if not pt:
            return False
        try:
            self.click_at_point(pt)
        except CDPError:
            return False
        return True

    def _input_text_now(self, sel: str) -> str:
        expr = (
            f"(() => {{ const e=document.querySelector({json.dumps(sel)});"
            f" if(!e) return '';"
            f" return (e.value !== undefined ? e.value : e.innerText || ''); }})()"
        )
        try:
            return self.cdp.eval_js(expr, timeout=10) or ""
        except CDPError:
            return ""

    def clear_input(self, sel: str) -> None:
        """清空输入框，避免上一条用例残留文本被一并发送。"""
        expr = f"""
        (() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el) return false;
          el.focus();
          if (el.value !== undefined) {{
            const isTA = el.tagName === 'TEXTAREA';
            const proto = isTA ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, '');
          }} else {{
            el.innerText = '';
          }}
          el.dispatchEvent(new Event('input', {{ bubbles: true }}));
          return true;
        }})()
        """
        try:
            self.cdp.eval_js(expr, timeout=15)
        except CDPError:
            pass
        time.sleep(0.2)

    def type_text(self, text: str) -> None:
        """输入文本：优先走浏览器编辑管线（对 React 受控组件友好），失败则回退。

        输入结果会被校验，未真正写入时直接抛错，避免「静默空发送」污染用例结果。
        """
        sel = self._resolve_input()
        if not sel:
            info = self.dump_dom_snapshot()
            raise RuntimeError(
                f"未能定位输入框（现场已导出 {info['path']}）：{info['summary']}")
        # 缓存的选择器必须仍指向真实可见的输入框：视图切换后 React 重挂载，
        # 旧选择器可能指向已被卸载的元素，焦点"成功"但发送会落到虚空。
        if not self._focus_input(sel):
            self._input_selector = None
            sel = self._resolve_input()
            if not sel or not self._focus_input(sel):
                raise RuntimeError(f"输入框不可见，无法聚焦：{sel}")

        self.clear_input(sel)
        self.cdp.insert_text(text)
        time.sleep(0.4)
        if text[:12] in self._input_text_now(sel):
            return

        # 回退 1：先发一次**真实鼠标点击**再走编辑管线。
        # `Input.insertText` 在部分 Windows 构建上会静默无效（受控组件没进入编辑态），
        # 而真实点击带来 user activation，输入才会真正过应用自己的编辑管线。
        # 这一层顺序很关键：直接改 DOM value（回退 2）虽然能把字塞进去，
        # 却绕过了应用内部状态 —— 发送按钮可能仍处于禁用态，
        # 表现为「输入框里明明有字，就是发不出去」。
        if self._click_selector(sel):
            self._focus_input(sel)
            self.cdp.insert_text(text)
            time.sleep(0.4)
            if text[:12] in self._input_text_now(sel):
                return

        # 回退 2（最后手段）：原生 setter + 手动派发 InputEvent，绕过 React 的 value 劫持
        payload = json.dumps(text)
        expr = f"""
        (() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el) return false;
          const isTA = el.tagName === 'TEXTAREA';
          const isInput = el.tagName === 'INPUT';
          if (isTA || isInput) {{
            const proto = isTA ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, {payload});
          }} else {{
            el.innerText = {payload};
          }}
          // 用 InputEvent 而不是裸 Event：带 inputType / data 的事件才会被富文本编辑器
          // 和「按输入类型判定能否发送」的受控组件识别（裸 Event 常被直接忽略）。
          let ev;
          try {{
            ev = new InputEvent('input', {{bubbles: true, inputType: 'insertText',
                                           data: {payload}}});
          }} catch (err) {{
            ev = new Event('input', {{bubbles: true}});
          }}
          el.dispatchEvent(ev);
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return true;
        }})()
        """
        self.cdp.eval_js(expr, timeout=15)
        time.sleep(0.3)

        landed = self._input_text_now(sel)
        if text[:12] not in landed:
            info = self.dump_dom_snapshot("ui_dom_input_failed.json")
            raise RuntimeError(
                f"文本未写入输入框（当前内容 {landed[:40]!r}），已中止发送；"
                f"现场已导出 {info['path']}：{info['summary']}")
        print("[应用] 文本是靠「直接改写页面内容」写进去的：应用自身的输入逻辑没有生效，"
              "如果发送按钮一直是灰的，这次发送会失败", flush=True)
        self._input_selector = None   # 视图切换后重新定位更稳妥

    # ------------------------------------------------------------ 附件投递
    # 被测应用的「引用本地文件」本质是**绝对路径列表**（Electron 31 的 File.path），
    # 上限 10 个，支持 PDF/PPT/Word/图片/Excel/CSV/文本/HTML/JSON/Markdown。
    #
    # 为什么不用 DOM.setFileInputFiles：应用的文件选择入口是 Electron 原生选择框
    # （前端桥接 API di.chooseLocalFile，网页端兜底实现就是 window.prompt 问路径），
    # **不走 Chromium 的 file chooser**，页面里也没有 <input type="file">，
    # 所以 CDP 的 setInterceptFileChooserDialog / setFileInputFiles 都够不着它。
    # 而应用同时支持拖拽，且拖拽与粘贴共用同一 handler → 用 dispatchDragEvent 投递。
    # 实测：拖拽后 title 由「引用本地文件 (0/10)」变为「(1/10)」，即已引用成功。
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

    def attach_count(self) -> Optional[int]:
        """读「引用本地文件」当前引用数；读不到返回 None（按钮不在页面上）。"""
        expr = f"""(() => {{
          const b = document.querySelector({json.dumps(ATTACH_BUTTON_SELECTOR)});
          if (!b) return null;
          const m = /[(](\\d+)\\s*[/]\\s*\\d+[)]/.exec(b.getAttribute('title') || '');
          return m ? Number(m[1]) : null;
        }})()"""
        try:
            return self.cdp.eval_js(expr, timeout=10)
        except CDPError:
            return None

    def _wait_attach_count(self, target: int, timeout_s: float) -> bool:
        deadline = time.time() + max(1.0, timeout_s)
        while time.time() < deadline:
            cur = self.attach_count()
            if cur is not None and cur >= target:
                return True
            time.sleep(0.4)
        return False

    def _composer_drop_point(self) -> Optional[dict]:
        """拖拽落点：输入区中心偏上（避开发送/工具栏按钮，防误触）。"""
        expr = f"""(() => {{
          const el = document.querySelector({json.dumps(COMPOSER_SELECTOR)})
                  || document.querySelector('textarea.chat-input-textarea');
          if (!el) return null;
          const r = el.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) return null;
          return {{x: Math.round(r.x + r.width / 2),
                   y: Math.round(r.y + r.height * 0.35)}};
        }})()"""
        try:
            return self.cdp.eval_js(expr, timeout=10)
        except CDPError:
            return None

    def _drag_files(self, paths: list) -> None:
        """按 dragEnter → dragOver → drop 完整序列投递（Chromium 要求按序）。"""
        pt = self._composer_drop_point()
        if not pt:
            raise CDPError("找不到输入区，无法投递附件")
        data = {
            "items": [{"mimeType": "text/plain", "data": ""}],
            "files": [str(p) for p in paths],
            "dragOperationsMask": 1,            # 1 = copy
        }
        for ev in ("dragEnter", "dragOver", "drop"):
            self.cdp.call("Input.dispatchDragEvent",
                          {"type": ev, "x": pt["x"], "y": pt["y"], "data": data},
                          timeout=15)
            time.sleep(0.35)

    def _clipboard_image_macos(self, path: str) -> tuple:
        """把图片文件写入系统剪贴板（macOS：osascript）。"""
        # 用 osascript 的 argv 接收路径：路径是独立 argv 元素，不参与脚本
        # 字符串解析，从根本上关闭「文件名含特殊字符 → AppleScript 注入」的面。
        script = (
            'on run argv\n'
            '    set the clipboard to (POSIX file (item 1 of argv))\n'
            'end run\n'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script, str(path)],
                check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=15,
            )
        except Exception as exc:
            return False, f"写入系统剪贴板失败：{type(exc).__name__}: {exc}"
        return True, ""

    def _clipboard_image_windows(self, path: str) -> tuple:
        """把图片文件写入系统剪贴板（Windows：PowerShell + WinForms）。

        路径经环境变量传入、不进脚本字符串 —— 与 osascript 的 argv 注入
        防护同思路，规避引号/特殊字符转义问题。-STA 是剪贴板 API 的
        线程要求（PS 5.1+ 默认 STA，显式指定以兼容旧版本）。
        标准位图格式由系统托管，PowerShell 退出后剪贴板内容仍然有效。
        """
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$img=[System.Drawing.Image]::FromFile($env:RUIYUN_CLIP_IMG);"
            "[System.Windows.Forms.Clipboard]::SetImage($img);"
            "$img.Dispose()"
        )
        env = dict(os.environ)
        env["RUIYUN_CLIP_IMG"] = str(path)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                env=env, check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=20,
            )
        except Exception as exc:
            return False, f"写入系统剪贴板失败：{type(exc).__name__}: {exc}"
        return True, ""

    def _paste_image(self, path: str) -> tuple:
        """备选投递：把图片写入系统剪贴板，再向输入框发粘贴键（macOS / Windows）。

        应用对**粘贴图片**有专门落盘兜底（savePastedImage），拿不到路径也能引用；
        文档类粘贴没有这层兜底，所以只在拖拽未生效且文件是图片时才走这里。
        """
        if sys.platform == "win32":
            ok, err = self._clipboard_image_windows(path)
        else:
            ok, err = self._clipboard_image_macos(path)
        if not ok:
            return False, err
        sel = self._resolve_input(wait_s=5)
        if not sel:
            return False, "找不到输入框，无法粘贴"
        self._focus_input(sel)
        time.sleep(0.2)
        # 粘贴键修饰符按平台取：macOS Cmd+V（Meta=4），Windows/Linux Ctrl+V（Ctrl=2）
        key = {"modifiers": 2 if sys.platform == "win32" else 4, "key": "v",
               "code": "KeyV", "windowsVirtualKeyCode": 86,
               "nativeVirtualKeyCode": 86 if sys.platform == "win32" else 9}
        try:
            self.cdp.call("Input.dispatchKeyEvent",
                          {"type": "rawKeyDown", "commands": ["paste"], **key}, timeout=10)
            self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **key}, timeout=10)
        except CDPError as exc:
            return False, f"粘贴事件发送失败：{exc}"
        return True, "已发送粘贴指令"

    def attach_files(self, paths: list, timeout_s: float = 8.0) -> tuple:
        """把本地文件投递给输入区（模拟真实用户的拖拽附件）。

        返回 (ok, message)。**判定以「引用计数是否真的增加」为准** ——
        只看有没有异常会漏掉「拖了但没进去」，那会让用例在完全没有附件的情况下跑完
        （假通过）。投递失败时按约定**仍继续发送**，由调用方把 message 写进 ui_error。

        路由：拖拽（覆盖文档类）→ 仍失败且含图片时再试剪贴板粘贴。
        """
        wanted = [str(p) for p in (paths or []) if str(p).strip()]
        if not wanted:
            return True, ""
        missing = [p for p in wanted if not Path(p).is_file()]
        if missing:
            return False, "附件不存在，未投递：" + "、".join(missing)

        before = self.attach_count()
        if before is None:
            return False, "无法读取「引用本地文件」计数（附件按钮不在页面上）"

        errs: list = []
        try:
            self._drag_files(wanted)
        except (CDPError, TimeoutError) as exc:
            errs.append(f"拖拽投递异常：{exc}")
        if self._wait_attach_count(before + len(wanted), timeout_s):
            return True, f"已引用 {self.attach_count()} 个本地文件（本轮投递 {len(wanted)} 个）"

        imgs = [p for p in wanted if Path(p).suffix.lower() in self.IMAGE_EXTS]
        if imgs:
            ok, msg = self._paste_image(imgs[0])
            if not ok:
                errs.append(msg)
            elif self._wait_attach_count(before + 1, timeout_s):
                return True, f"已通过粘贴引用 1 个图片文件（{Path(imgs[0]).name}）"

        after = self.attach_count()
        return False, (
            f"附件未投递成功（引用计数未增加：投递前 {before}、"
            f"投递后 {after if after is not None else '未知'}，期望 +{len(wanted)}）"
            + ("；" + "；".join(errs) if errs else ""))

    # ------------------------------------------------------------ 发送
    def reset_to_new_task(self, wait_s: float = 15.0) -> bool:
        """回到「新建任务」首页。

        关键：首次发送后应用会进入会话视图，此时再发送会追加到同一会话而
        不会产生新会话目录。每条用例前必须回到首页，才能保证一例一会话。
        """
        expr = r"""
        (() => {
          const btns = [...document.querySelectorAll('button')];
          const b = btns.find(x => ((x.innerText||'').trim().includes('新建任务'))
                        || ((x.className||'').toString().includes('sidebar-primary-action')));
          if (!b) return null;
          b.click();
          return (b.innerText||'').trim().slice(0, 20);
        })()
        """
        try:
            if not self.cdp.eval_js(expr, timeout=15):
                return False
        except CDPError:
            return False

        # 等待首页输入框重新可用
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self._composer_ready():
                time.sleep(0.4)
                return True
            time.sleep(0.5)
        return False

    def _composer_ready(self) -> bool:
        expr = (
            "(() => { const e=document.querySelector('textarea');"
            " if(!e) return false; const r=e.getBoundingClientRect();"
            " return r.width>0 && r.height>0; })()"
        )
        try:
            return bool(self.cdp.eval_js(expr, timeout=10))
        except CDPError:
            return False

    # 发送按钮探测/点击：同一段脚本，用 click 开关区分「真点」与「只看」。
    # 关键改动：**跳过 disabled / aria-disabled 候选**（点了也不会发，旧实现却把它
    # 记成一次成功点击），并在一个都点不了时如实返回 disabled:<按钮描述>。
    _SEND_BUTTON_JS = r"""
    (() => {
      const kws = %s;
      const click = %s;
      const cands = [...document.querySelectorAll('button,[role="button"],div,span,svg')];
      let disabledSeen = null;
      for (const el of cands) {
        const blob = ((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'')
                      + ' ' + (el.className||'').toString() + ' ' + (el.getAttribute('title')||'')).toLowerCase();
        if (!kws.some(k => blob.includes(k))) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.width > 260 || r.height > 120) continue;   // 排除容器
        const btn = el.closest('button,[role="button"]') || el;
        const label = ((btn.innerText||'') + '/' + (btn.getAttribute('aria-label')||'')
                       + '/' + (btn.getAttribute('title')||'') + '/'
                       + ((btn.className||'').toString())).replace(/\s+/g, ' ').trim().slice(0, 40)
                      || 'clicked';
        const dis = !!btn.disabled || btn.getAttribute('aria-disabled') === 'true';
        if (dis) { if (!disabledSeen) disabledSeen = 'disabled:' + label; continue; }
        if (click) btn.click();
        return label;
      }
      return disabledSeen;
    })()
    """

    def _send_button(self, click: bool) -> str:
        """探测（或点击）发送按钮，返回按钮描述；返回 disabled:xxx 表示存在但不可点。"""
        expr = self._SEND_BUTTON_JS % (json.dumps(SEND_KEYWORDS),
                                      "true" if click else "false")
        try:
            return str(self.cdp.eval_js(expr, timeout=15) or "")
        except CDPError:
            return ""

    def _click_send_button(self) -> str:
        """点击发送按钮，返回按钮描述（空串 = 没找到；disabled:* = 找到但不可点）。"""
        return self._send_button(click=True)

    def _input_text_now_any(self) -> str:
        """读当前输入框内容；选择器缓存失效时重新定位一次。"""
        sel = self._input_selector or self._resolve_input(wait_s=3)
        return self._input_text_now(sel) if sel else ""

    def _wait_input_cleared(self, timeout_s: float = 1.5) -> bool:
        """等待输入框被清空 —— 「消息真的发出去了」最直接的信号。"""
        deadline = time.time() + max(0.3, timeout_s)
        while time.time() < deadline:
            if not self._input_text_now_any().strip():
                return True
            time.sleep(0.2)
        return False

    def send(self) -> str:
        """发送：按钮 → 回车 → 带 char 的回车，逐级校验「输入框是否真的被清空」。

        旧实现只要发出过一次 click 就返回 button，于是「点到容器 / 点到禁用按钮 /
        应用没进入可发送态」统统被当成已发送，一路等到「未产生新会话」才失败，
        控制台里看不到真实原因。现在以输入框是否真的清空作为判据，逐级降级：

          1) 点发送按钮（跳过 disabled 候选）；
          2) 回车 —— 先按旧口径只发 rawKeyDown + keyUp；
          3) 仍没生效才补发 char 事件。靠 keypress / beforeinput 提交的应用
             只有这一发能收到（Chromium 在 text 为空时不合成字符事件）。

        分级而不是「一上来就带 char」的原因：对「keydown 即发送」的应用，
        多余的一次字符事件会在已清空的输入框里插入一个换行。
        """
        landed = self._input_text_now_any()
        desc = self._click_send_button()
        clicked = bool(desc) and not desc.startswith("disabled:")
        if clicked and (not landed.strip() or self._wait_input_cleared(1.5)):
            return f"button({desc})"

        self.cdp.press_key("Enter")
        if not landed.strip() or self._wait_input_cleared(1.2):
            return "enter"

        self.cdp.press_key("Enter", text="\r")
        if landed.strip():
            self._wait_input_cleared(1.2)
        return "enter(char)" if clicked or not desc else f"enter(按钮不可点:{desc})"

    def composer_state(self) -> dict:
        """输入区与发送按钮的现状快照。

        发送后没产生会话时，流水线会把它写进控制台 —— 「字根本没进去」
        与「有字但发不出去（按钮不可点）」是两类完全不同的故障，必须能区分，
        否则只能看到一句笼统的「未产生新会话」。
        """
        sel = self._input_selector or self._resolve_input(wait_s=3)
        text = self._input_text_now(sel) if sel else ""
        return {
            "input_selector": sel or "",
            "text_len": len(text or ""),
            "text_head": (text or "")[:40],
            "send_button": self._send_button(click=False),
        }

    def page_state(self) -> dict:
        """页面现状快照：URL/标题、是否登录页、有没有输入区与新任务按钮。

        全新 Windows 安装很可能停在登录页 —— 那时页面里根本没有对话输入框，
        表现为「无法回到新建任务首页 / 未能定位输入框」。把登录页判断出来并
        显示正文片段，比让人对着「找不到输入框」猜要有效得多。
        """
        expr = r"""
        (() => {
          const vis = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const body = (document.body ? document.body.innerText : '') || '';
          const loginRe = /(登录|扫码|验证码|账号|密码|sign\s*in|log\s*in)/i;
          const pwd = [...document.querySelectorAll('input[type="password"]')].some(vis);
          const composer = [...document.querySelectorAll(
            'textarea,[contenteditable="true"],[role="textbox"]')].some(vis);
          const newTask = [...document.querySelectorAll('button')].some(b =>
            ((b.innerText || '').includes('新建任务')
             || ((b.className || '').toString().includes('sidebar-primary-action'))));
          return {
            href: (location.href || '').slice(0, 160),
            title: (document.title || '').slice(0, 80),
            has_password_input: pwd,
            has_composer: composer,
            has_new_task: newTask,
            login_like: pwd || loginRe.test(body),
            body_head: body.replace(/\s+/g, ' ').slice(0, 120),
          };
        })()
        """
        try:
            return self.cdp.eval_js(expr, timeout=15) or {}
        except CDPError:
            return {}

    # ------------------------------------------------------------ 自动确认
    # 卡片内容完全由 LLM 生成，选项文本不可枚举，因此两层检测：
    #   1) 关键词优先：按钮文本命中 confirm_keywords → 精确点击；
    #   2) 结构兜底：同父容器下 ≥2 个带文本的可见按钮 = 选项组 → 点第一个
    #      非拒绝项（拒绝/取消会让任务终止，违背「完整跑完」目标）。
    # 排除侧边栏与输入工具栏（常驻按钮，非确认）；拒绝词永不点击。
    _AUTO_CONFIRM_JS_TEMPLATE = r"""
    (() => {
      const prefer = %s;
      const deny   = %s;
      // 导航/Tab 类组件永不是选项（实测首页 Tab: home-page__tab / tabs）
      const navRe = /(^|[\s_-])(tab|tabs|nav|menu|toolbar|panel)([\s_-]|$)/i;
      const inChatArea = (el) => {
        for (let n = el; n; n = n.parentElement) {
          const c = (n.className || '').toString();
          if (c.includes('sidebar') || c.includes('chat-toolbar')) return false;
          // 选项卡（agent-question-composer）有专属事实驱动路径，通用路径一律不碰：
          // 否则「提交中」期间仍会点到卡片里的禁用选项，制造假事件。
          if (c.includes('agent-question')) return false;
        }
        return true;
      };
      const isDeny = (t) => deny.some(w => t.includes(w));
      const visibleText = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return null;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 60) return null;
        return t;
      };
      const els = [...document.querySelectorAll('button,[role="button"]')]
        .filter(el => inChatArea(el));

      // 1) 关键词路径（拒绝词永不点；导航类组件永不点）
      for (const el of els) {
        const t = visibleText(el);
        const c = (el.className || '').toString();
        if (t && !isDeny(t) && !navRe.test(t) && !navRe.test(c)
            && prefer.some(k => t.includes(k))) {
          return {text: t.slice(0, 40), cls: (el.className || '').toString().slice(0, 80),
                  mode: 'keyword'};
        }
      }
      // 2) 结构路径：选项组（同父 ≥2 个带文本按钮）→ 第一个非拒绝项
      const byParent = new Map();
      for (const el of els) {
        const t = visibleText(el);
        if (!t) continue;
        const p = el.parentElement;
        if (!p) continue;
        if (!byParent.has(p)) byParent.set(p, []);
        byParent.get(p).push({el, t, cls: (el.className || '').toString()});
      }
      for (const [p, group] of byParent) {
        const pCls = (p.className || '').toString();
        if (group.length >= 2 && !navRe.test(pCls)
            && !group.some(g => navRe.test(g.cls))) {
          // 拒绝过滤放在选择时：避免把「取消 + 继续」这类二元组误判成单按钮
          const pick = group.find(g => !isDeny(g.t));
          if (!pick) continue;                     // 整组都是拒绝类 → 不点
          return {text: pick.t.slice(0, 40),
                  cls: (pick.el.className || '').toString().slice(0, 80),
                  parentCls: (p.className || '').toString().slice(0, 80),
                  texts: group.map(g => g.t.slice(0, 30)),
                  mode: 'first-option'};
        }
      }
      return null;
    })()
    """

    _CONFIRM_FAIL_LIMIT = 5          # 同一卡片连续点击失败 N 次 → 请求人工介入
    _CONFIRM_RETRY_COOLDOWN = 15     # 点击未生效后，隔 N 秒重试
    _CONFIRM_HUMAN_JS = None

    # --------------------------------------------------- 事件归属（会话）
    @staticmethod
    def _sess_key(sess) -> str:
        """会话标识规范化：统一成绝对路径字符串（与 case.session_dir 同源）。"""
        if not sess:
            return ""
        try:
            return str(Path(sess))
        except Exception:
            return str(sess)

    def set_current_sess(self, sess) -> None:
        """记录当前在途会话：流水线在「新会话目录出现」后调用。

        并发下卡片只渲染在当前可见会话，点击发生在哪条会话只有此刻知道，
        因此归属必须在驱动层落痕；事后再按时间窗推断必然串台。
        """
        self._current_sess = self._sess_key(sess)

    def _owner_sess(self, explicit=None) -> str:
        """本次点击的归属会话：显式传入（巡检）> 当前视图 > 当前在途会话。

        巡检 cycle_and_confirm 会把视图切到目标会话，故 _qcard_view_sess 就是
        点击时真正可见的那条会话，优先级高于「最近一次发送的会话」。
        """
        return (self._sess_key(explicit) or self._sess_key(self._qcard_view_sess)
                or self._sess_key(self._current_sess) or "")

    def _confirm_fail(self, msg: str) -> None:
        """记一次自动确认失败；连续失败达到上限 → 停用并请求人工介入。"""
        self._confirm_cdp_streak += 1
        cite_emit(f"[确认] 第 {self._confirm_cdp_streak} 次失败：{msg}")
        if self._confirm_cdp_streak >= self._CONFIRM_FAIL_LIMIT:
            self.auto_confirm = False
            self.confirm_human_needed = True
            self.confirm_human_sess = self._owner_sess()
            cite_emit(f"[确认] 连续 {self._CONFIRM_FAIL_LIMIT} 次失败，已停用自动确认，"
                      "需要你在应用界面手动点一下确认卡片")

    # 选项卡（agent-question-composer）专用探测：**一次往返取回全部事实**。
    #
    # 为什么必须一次取全：单选且非末题时「点选项会立即跳到下一题」，多次 eval
    # 之间状态会漂移；把题号/选项/按钮一次性读回，后续决策只依据这一份快照。
    #
    # 依据 app.asar 内组件源码（非猜测）：
    #   <section class="agent-question-composer" role="dialog">，题号在
    #   .agent-question-composer__counter（形如 "1/4"），选项是
    #   button.agent-question-option（选中态为附加类 is-selected），底部按钮三态：
    #     末题          → .agent-question-composer__primary，文案「提交」
    #     非末题已作答  → .agent-question-composer__primary，文案「确认」
    #     非末题未作答  → .agent-question-composer__skip，文案「跳过」（永不可点）
    #   disabled 表示「提交中」（此时选项与底部按钮一起禁用，文案变「提交中…」）。
    _QCARD_JS = r"""
    (() => {
      const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const clsList = (el) => ((el && el.className) || '').toString().split(/\s+/);
      const txt = (el) => ((el && el.innerText) || '').trim();
      // 只认真正的容器类（子元素是 agent-question-composer__body 这类前缀名），
      // 并取最后一个：历史残影在前、当前卡片在后。
      const roots = [...document.querySelectorAll('[class*="agent-question-composer"]')]
        .filter(el => clsList(el).includes('agent-question-composer') && vis(el));
      if (!roots.length) return {found: false};
      const composer = roots[roots.length - 1];

      const titleEl = composer.querySelector('#agent-question-title')
                  || composer.querySelector('.agent-question-composer__heading h2');
      const counter = txt(composer.querySelector('.agent-question-composer__counter'));
      const m = /^(\d+)\s*\/\s*(\d+)$/.exec(counter);
      const questionIndex = m ? Number(m[1]) - 1 : null;
      const questionCount = m ? Number(m[2]) : null;

      const options = [...composer.querySelectorAll('button.agent-question-option')].map(el => ({
        label: (txt(el.querySelector('.agent-question-option__label')) || txt(el)).slice(0, 80),
        selected: clsList(el).includes('is-selected'),
        disabled: !!el.disabled,
        visible: vis(el),
      }));

      const otherInput = composer.querySelector('.agent-question-other input');
      const customInput = otherInput ? (otherInput.value || '') : null;
      const inputDisabled = otherInput ? !!otherInput.disabled : true;

      const buttons = [
        ...composer.querySelectorAll('.agent-question-composer__footer button'),
        ...composer.querySelectorAll('.agent-question-composer__nav button'),
      ].map(el => {
        const c = (el.className || '').toString();
        const kind = c.includes('composer__primary') ? 'primary'
                   : c.includes('composer__skip') ? 'skip'
                   : c.includes('composer__close') ? 'close' : 'nav';
        return {kind, text: txt(el).slice(0, 20), disabled: !!el.disabled, visible: vis(el)};
      });

      const primary = buttons.find(b => b.kind === 'primary' && b.visible) || null;
      const skip = buttons.find(b => b.kind === 'skip' && b.visible) || null;
      // 「提交中」没有独立标志位：文案变「提交中…」或 primary 与全部选项一起禁用
      const submitting = !!(primary && (
        primary.text.includes('提交中')
        || (primary.disabled && options.length > 0 && options.every(o => o.disabled))
      ));

      return {
        found: true, question: txt(titleEl).slice(0, 150), counter,
        questionIndex, questionCount,
        isLast: (questionIndex !== null && questionCount !== null)
                  ? questionIndex === questionCount - 1 : null,
        options, customInput, inputDisabled, submitting, primary, skip,
      };
    })()
    """

    # 点击执行：kind ∈ option（第 idx 个选项）/ primary（确认·提交）/ fill-other（自由输入题）。
    # 与旧实现的关键差别：**点击前判 disabled / aria-disabled，点不到就如实返回 ok=false** ——
    # 旧版只判可见性，「提交中」的禁用按钮也会被记成一次成功点击，制造假事件洪水。
    _QCARD_CLICK_JS = r"""
    (() => {
      const kind = %s;
      const idx  = %s;
      const text = %s;
      const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const clsList = (el) => ((el && el.className) || '').toString().split(/\s+/);
      const roots = [...document.querySelectorAll('[class*="agent-question-composer"]')]
        .filter(el => clsList(el).includes('agent-question-composer') && vis(el));
      if (!roots.length) return {ok: false, reason: 'no-card'};
      const composer = roots[roots.length - 1];

      let el = null;
      if (kind === 'primary') {
        el = composer.querySelector('button.agent-question-composer__primary');
      } else if (kind === 'option') {
        el = [...composer.querySelectorAll('button.agent-question-option')][idx] || null;
      } else if (kind === 'fill-other') {
        el = composer.querySelector('.agent-question-other input');
      }
      if (!el) return {ok: false, reason: 'no-target'};
      if (!vis(el)) return {ok: false, reason: 'invisible'};
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
        return {ok: false, reason: 'disabled', text: (el.innerText || '').trim().slice(0, 30)};
      }
      if (kind === 'fill-other') {
        // 走原生 setter + input 事件，绕过 React 的 value 劫持
        const proto = window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, text);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return {ok: true, kind: kind, text: text.slice(0, 30)};
      }
      el.click();
      return {ok: true, kind: kind, text: ((el.innerText || '') + '').trim().slice(0, 40)};
    })()
    """

    def _qcard_reset(self) -> None:
        """卡片消失：清空进度状态并解除会话锁定。"""
        self._qcard_fp_sig = ""
        self._qcard_fp_since = 0.0
        self._qcard_fp_clicks = {}
        self._qcard_card_clicks = 0
        self._qcard_card_since = 0.0
        self._qcard_last_click = 0.0
        self._qcard_owner_sess = None

    def _qcard_stop(self, why: str) -> None:
        """判定「点不动」：立刻停用自动确认并转人工，不再硬点到熔断。

        旧实现只在点击失败 5 次或累计 200 次点击时才停手，卡片一旦不响应就会
        空转上百次才转人工；这里用「同指纹尝试上限 / 停滞窗口 / 卡片生命期」三重
        口径，几秒内就止损。
        """
        self.auto_confirm = False
        self.confirm_human_needed = True
        self.confirm_human_sess = self._owner_sess()
        cite_emit(f"[确认] {why}，已停用自动确认，需要你在应用界面手动选择")

    def _qcard_facts(self) -> Optional[dict]:
        """读一次卡片事实（只读、单次往返）；CDP 异常由调用方处理。"""
        return self.cdp.eval_js(self._QCARD_JS, timeout=10) or None

    def _qcard_click(self, kind: str, idx: int = 0, text: str = "") -> dict:
        """执行一次点击，返回 {ok, reason?, text?}（未生效一律 ok=False）。"""
        js = self._QCARD_CLICK_JS % (
            json.dumps(kind, ensure_ascii=False),
            json.dumps(int(idx)),
            json.dumps(text, ensure_ascii=False),
        )
        try:
            out = self.cdp.eval_js(js, timeout=10)
        except Exception as exc:
            return {"ok": False, "reason": f"eval-error:{type(exc).__name__}"}
        return out if isinstance(out, dict) else {"ok": False, "reason": "no-result"}

    @staticmethod
    def _qcard_fp(f: dict) -> str:
        """进度指纹：题号 + 题目 + 选项标签与选中态 + 底部按钮形态。

        指纹变化 == 真的推进（换题 / 选中态变化 / 按钮从跳过变确认）。
        """
        if not f or not f.get("found"):
            return "(无卡片)"
        opts = "|".join(
            f"{o.get('label', '')}{'#' if o.get('selected') else ''}"
            for o in (f.get("options") or []))
        btn = f.get("primary") or f.get("skip") or {}
        return f"{f.get('counter')}::{f.get('question', '')}::{opts}::{btn.get('kind')}"

    def _qcard_emit(self, mode: str, facts: dict, res: dict,
                    owner_sess=None) -> Optional[dict]:
        """点击结果统一出口：**只有真的点下去才记事件**，未生效则计一次无效尝试。

        owner_sess：本次点击所属会话（巡检时由调用方显式传入；主视图扫描留空，
        由 _owner_sess 按「当前视图 → 当前在途会话」兜底）。事件带上归属才能
        在轮次详情里混进对应用例的时间线。
        """
        fp = self._qcard_fp(facts)
        self._qcard_fp_clicks[fp] = self._qcard_fp_clicks.get(fp, 0) + 1
        self._qcard_last_click = time.time()
        q = str(facts.get("counter") or "")
        question = str(facts.get("question") or "")[:150]
        if not res.get("ok"):
            cite_emit(cite_line(mode, q, question,
                                extra=f"没点动：{res.get('reason')}"))
            return None
        self._qcard_card_clicks += 1
        ev = {
            "time": time.strftime("%H:%M:%S"), "ts": time.time(),
            "mode": mode, "text": str(res.get("text") or "")[:40],
            "cls": ("agent-question-option" if mode == "qcard-option"
                    else "agent-question-other" if mode == "qcard-fill"
                    else "agent-question-composer__primary"),
            "q": q,
            # 题干原文：轮次详情展开这条记录时「问题是什么」就靠它。
            # 旧产物没有该字段，前端回退为只显示题号。
            "question": question,
            "sess": self._owner_sess(owner_sess),
        }
        self.confirm_events.append(ev)     # 全流程留痕（含题型/答案，供时间线还原）
        cite_emit(cite_line(mode, q, question, answer=str(ev["text"] or "")))
        return ev

    def _qcard_handle(self, owner_sess=None) -> Optional[dict]:
        """选项卡推进：**每个轮询周期现读 DOM 事实，再按事实决定点哪里**。

        规则（替代旧的「无条件点第一个选项 → 点 primary」两段式）：
          1. 提交中（按钮禁用）→ 只等待，绝不点击、绝不记事件；
          2. 本题未作答 → 点第一个可用选项；纯自由输入题则填一句中性回答；
          3. 本题已作答 → 点 primary（末题=提交，非末题=确认进入下一题）；
          4. 「跳过」「×」「上一题/下一题」永不可点。
        三重止损：同一指纹点击上限 / 卡片累计点击上限 / 停滞与生命期窗口。

        owner_sess：调用方所处的会话目录。传入时，一旦发现卡片就把它记为
        **卡片所有者**，供巡检层锁视图 —— 实测切换会话视图会卸载卡片组件、
        清空已选答案，使卡片退回第 1 题（scripts/probe_qcard.py --switch-test）。
        """
        if not self.cdp:
            return None
        try:
            f = self._qcard_facts()
        except Exception:
            return None
        if not f or not f.get("found"):
            self._qcard_reset()
            return None
        if owner_sess is not None and self._qcard_owner_sess is None:
            self._qcard_owner_sess = owner_sess

        now = time.time()
        if not self._qcard_card_since:
            self._qcard_card_since = now
        fp = self._qcard_fp(f)
        if fp != self._qcard_fp_sig:
            # 指纹变化 = 真实推进：换指纹、重置停滞计时，并允许立刻点下一格
            self._qcard_fp_sig = fp
            self._qcard_fp_since = now
            self._qcard_last_click = 0.0
        # 止损一：同一指纹反复点（含 A→B→A 抖动）
        if self._qcard_fp_clicks.get(fp, 0) >= self._qcard_same_limit:
            self._qcard_stop(f"同一状态已尝试 {self._qcard_same_limit} 次仍未推进"
                             f"（题 {f.get('counter') or '?'}）")
            return None
        # 止损二：本张卡片累计点击 / 存活时长超限
        if self._qcard_card_clicks >= self._qcard_card_clicks_limit:
            self._qcard_stop(f"本张卡片累计点击 {self._qcard_card_clicks} 次仍未完成")
            return None
        if now - self._qcard_card_since > self._qcard_card_max_s:
            self._qcard_stop(f"卡片存活超过 {self._qcard_card_max_s:.0f}s 仍未完成")
            return None
        # 止损三：停滞窗口内指纹不变（例如一直提交中、或点下去毫无反应）
        if now - self._qcard_fp_since > self._qcard_stall_s:
            self._qcard_stop(f"停滞 {self._qcard_stall_s:.0f}s 无进展"
                             f"（题 {f.get('counter') or '?'}）")
            return None
        # 节流：与上一次尝试至少间隔 min_gap（防 0.5s 轮询连点）
        if now - self._qcard_last_click < self._qcard_min_click_gap:
            return None
        if f.get("submitting"):
            return None                     # 提交中：点了也无效，等应用处理完

        opts = [o for o in (f.get("options") or [])
                if o.get("visible") and not o.get("disabled")]
        answered = any(o.get("selected") for o in (f.get("options") or [])) \
            or bool((f.get("customInput") or "").strip())

        if not answered:
            if opts:
                return self._qcard_emit("qcard-option", f,
                                        self._qcard_click("option", 0),
                                        owner_sess=owner_sess)
            # 纯自由输入题（无选项）：填一句中性回答，避免无声挂死到等待上限
            if (f.get("customInput") is not None and not f.get("inputDisabled")
                    and not (f.get("customInput") or "").strip()):
                return self._qcard_emit(
                    "qcard-fill", f,
                    self._qcard_click("fill-other", 0, self._qcard_fill_text),
                    owner_sess=owner_sess)
            return None

        primary = f.get("primary") or {}
        if primary and not primary.get("disabled"):
            mode = "qcard-submit" if f.get("isLast") else "qcard-confirm"
            return self._qcard_emit(mode, f, self._qcard_click("primary", 0),
                                    owner_sess=owner_sess)
        return None

    def _maybe_auto_confirm(self, skip_qcard: bool = False,
                            qcard_owner=None) -> Optional[dict]:
        """扫描主对话区，发现确认卡片就处理。返回证据（无则 None）。

        - 选项卡（agent-question-composer）走专用事实驱动路径，优先处理；
        - 其余卡片走两层检测：关键词优先 → 选项组结构兜底（卡片文案由 LLM 生成，
          不可枚举，纯文本匹配必然漏）；
        - 同一卡片 15s 后重试；连续 5 次点击卡片仍在 → 停用并标记需人工介入；
        - CDP 异常时先重连一次再试，连续 5 次仍失败同样请求人工介入。
        skip_qcard=True：本轮已单独推进过选项卡，跳过卡片路径，避免同轮重复点击。
        """
        if not self.auto_confirm or not self.cdp:
            return None
        if len(self.confirm_events) >= self._confirm_max_clicks:
            self.auto_confirm = False
            self.confirm_human_needed = True
            self.confirm_human_sess = self._owner_sess(qcard_owner)
            cite_emit(f"[确认] 自动确认累计已达 {self._confirm_max_clicks} 次，已停用，"
                      "需要你在应用界面手动处理后续确认")
            return None
        # 专用路径：agent-question-composer 选项卡（事件已在 _qcard_emit 内留痕）
        if not skip_qcard:
            qev = self._qcard_handle(owner_sess=qcard_owner)
            if qev:
                return qev

        js = self._AUTO_CONFIRM_JS_TEMPLATE % (
            json.dumps(self.confirm_keywords, ensure_ascii=False),
            json.dumps(DENY_WORDS, ensure_ascii=False),
        )
        try:
            info = self.cdp.eval_js(js, timeout=10)
        except Exception:
            # 连接可能已失效：重连一次再试。
            # 注意 info 必须先初始化 —— 否则重连失败（attach 返回 False）时
            # info 未定义，下方读取会触发 UnboundLocalError 使整个测试进程崩溃。
            info = None
            try:
                self.detach()
                if not self.attach(timeout_s=15):
                    self._confirm_fail("CDP 连接异常（重连失败）")
                    return None
            except Exception:
                self._confirm_fail("CDP 连接异常（重连出错）")
                return None
            try:
                info = self.cdp.eval_js(js, timeout=10)
            except Exception:
                self._confirm_fail("CDP 连接异常（重连后仍失败）")
                return None

        if not info:
            self._confirm_cdp_streak = 0     # 页面无卡片，一切正常
            return None

        # 冷却：同一签名（结构路径=选项组文本集；关键词路径=点击文本）
        # 点击后 COOLDOWN 秒内不重复点 —— 给应用处理时间，也防刷点
        now = time.time()
        if info.get("mode") == "first-option":
            sig = "|".join(sorted(info.get("texts") or [info.get("text", "")]))
        else:
            sig = info.get("text", "")
        rec = self._clicked_groups.get(sig)
        if rec and now - rec["ts"] < self._confirm_cooldown:
            return None                      # 冷却期内
        rec = rec or {"attempts": 0}
        rec["ts"] = now
        rec["attempts"] = rec.get("attempts", 0) + 1
        self._clicked_groups[sig] = rec
        # 结构路径：同一选项组点击 5 次仍未消失 → 请求人工介入
        if (info.get("mode") == "first-option"
                and rec["attempts"] > 5):
            self.auto_confirm = False
            self.confirm_human_needed = True
            self.confirm_human_sess = self._owner_sess(qcard_owner)
            cite_emit("[确认] 同一个选项卡片点了 5 次仍未消失，已停用自动确认，"
                      "需要你在应用界面手动处理")
            return None

        ev = {"time": time.strftime("%H:%M:%S"), "ts": time.time(),
              "mode": info.get("mode", ""), "text": info.get("text", ""),
              "cls": info.get("cls", ""), "parentCls": info.get("parentCls", ""),
              "sess": self._owner_sess(qcard_owner)}
        self.confirm_events.append(ev)
        cite_emit(cite_line(str(info.get("mode") or ""),
                            question=str(info.get("question") or ""),
                            answer=str(ev["text"] or "")))
        time.sleep(0.8)        # 给 UI 反应时间
        return ev

    # ---------------------------------------------------- 多会话视图巡检
    # 并发下卡片只渲染在「当前可见会话」，后台会话弹卡无人看见。
    # 这里按需把视图依次切到每条在途会话，复用既有扫描/点击与全部护栏。
    # 前提（README 流水线实验已证实）：生成中切换视图不打断后台生成。
    def _norm_key(self, text) -> str:
        return "".join(str(text or "").split())

    def _sidebar_items(self):
        """读 sidebar 任务列表项。None=结构失效（触发降级）；[]=无可见项。"""
        js = r"""
        (() => {
          const aside = document.querySelector('aside.session-sidebar,aside[class*="session-sidebar"]');
          if (!aside) return null;
          const nodes = [...aside.querySelectorAll('.session-sidebar-task-item')];
          return nodes.map((el, idx) => {
            const btn = el.querySelector('.session-sidebar-task-item__button');
            const tm = el.querySelector('.session-sidebar-task-item__time');
            const r = el.getBoundingClientRect();
            return { idx,
              title: ((btn && btn.innerText) || '').trim(),
              time: ((tm && tm.innerText) || '').trim(),
              unread: !!el.querySelector('[class*="unread"]'),
              status: el.getAttribute('data-status-kind') || '',
              visible: r.width > 0 && r.height > 0 };
          }).filter(x => x.visible);
        })()
        """
        try:
            return self.cdp.eval_js(js, timeout=10)
        except Exception:
            return None

    def _find_item(self, items, prompt: str):
        """标题↔提示词双向前缀匹配列表项；未读点优先，同名会话按轮转去重。"""
        import re
        time_tail = re.compile(r"\s*(\d+\s*(?:分钟|小时|天)前|刚刚|昨天|前天)$")
        def norm(t):
            t = self._norm_key(t)
            for _ in range(2):                # 时间后缀 + 截断省略号（可能叠加）
                t = time_tail.sub("", t).rstrip(".。…")
            return t
        key = norm(prompt)
        if not key:
            return None
        cands = []
        for it in items:
            t = norm(it.get("title"))
            if not t:
                continue
            # 标题可能被截断（key 以 title 开头）；完整标题则 title == key[:len]
            if key.startswith(t) or t.startswith(key[:len(t)]):
                cands.append(it)
        if not cands:
            return None
        fresh = [it for it in cands if it.get("unread")]
        pool = fresh or [it for it in cands
                         if (it.get("title"), it.get("time")) not in self._cycle_visited]
        return (pool or cands)[0]

    def _open_conversation(self, item) -> bool:
        """点击 sidebar 列表项，切换到对应会话视图。"""
        js = """
        ((idx) => {
          const aside = document.querySelector('aside.session-sidebar,aside[class*="session-sidebar"]');
          const el = aside.querySelectorAll('.session-sidebar-task-item')[idx];
          if (!el) return null;
          const btn = el.querySelector('.session-sidebar-task-item__button') || el;
          btn.click();
          return (btn.innerText || '').trim().slice(0, 40);
        })(%d)
        """ % int(item["idx"])
        try:
            return bool(self.cdp.eval_js(js, timeout=10))
        except Exception:
            return False

    def cycle_and_confirm(self, pairs, remaining) -> int:
        """并发视图巡检：依次切到每条在途会话，扫描并处理确认卡片。

        pairs: [(sess_dir, res)]（最旧在途优先，由调用方保证）；
        remaining: 尚未判稳的会话目录集合。返回本轮成功点击的卡片数。

        **选项卡独占视图**：当前视图只要存在 agent-question-composer 卡片，本轮就
        只在本机推进该卡片、绝不切换会话；同理，一旦在某条会话里发现卡片，立即
        break 并锁定。原因：实测切换会话视图会卸载卡片组件、清空已选答案，卡片
        退回第 1 题（scripts/probe_qcard.py --switch-test）—— 旧实现每轮都切视图，
        于是每轮都在点第 1 题，表现为「反复点、状态不推进直到熔断转人工」。

        护栏：连续 3 轮读不到会话列表 → 停用巡检（退回仅当前视图扫描）；
        点击冷却与止损由 _qcard_handle / _AUTO_CONFIRM_JS_TEMPLATE 各自承担。
        """
        if not self.auto_confirm or not self.cdp or self.view_cycle_disabled:
            return 0
        todo = [(s, r) for s, r in pairs if s in remaining]
        if not todo:
            return 0
        # 0) 当前视图就有卡片 → 只在本机推进，不切换任何会话（切走即丢答案）
        try:
            cur = self._qcard_facts()
        except Exception:
            return 0          # 读不到卡片就静默跳过本轮，频繁报错只会刷屏
        if cur and cur.get("found"):
            # 「本轮不切换会话」是实现细节，用户不需要知道；点了什么由 _qcard_emit 报
            ev = self._qcard_handle(owner_sess=self._qcard_view_sess)
            return 1 if ev else 0

        items = self._sidebar_items()
        if items is None:
            self._cycle_fail_streak += 1
            if self._cycle_fail_streak >= 3:
                self.view_cycle_disabled = True
                cite_emit("[确认] 连续 3 轮读不到会话列表，已停用视图巡检，"
                          "改为只扫描当前会话（后台会话的确认卡片可能因此被漏掉）")
            return 0
        self._cycle_fail_streak = 0
        self._cycle_visited = set()
        clicked = 0
        for sess, res in todo:
            item = self._find_item(items, res.prompt)
            if item is None:
                continue
            self._cycle_visited.add((item.get("title"), item.get("time")))
            if not self._open_conversation(item):
                continue
            self._qcard_view_sess = sess      # 记录当前视图，供卡片锁定判断
            time.sleep(0.5)                   # 等会话视图渲染
            ev = self._qcard_handle(owner_sess=sess)   # 选项卡优先（事件已内部留痕）
            if ev:
                clicked += 1
            if self._qcard_owner_sess is not None:
                # 卡片必须留在可见会话里才能继续点（切走会重置成第 1 题），
                # 故本轮到此为止；无需再打一行日志说明。
                break
            # 授权卡等通用卡片：显式带上当前巡检会话，事件才不会串到别的用例
            ev = self._maybe_auto_confirm(skip_qcard=True, qcard_owner=sess)
            if ev:
                clicked += 1
        if clicked:
            cite_emit(f"[确认] 本轮切换 {len(self._cycle_visited)} 条会话，"
                      f"共确认 {clicked} 次")
        return clicked

    # ------------------------------------------------------------ 会话等待
    def snapshot_sessions(self) -> set:
        if not self.session_root.is_dir():
            return set()
        # 与 core.log_parser.discover_sessions 保持口径一致：只认 sess_* 会话目录，
        # 避免 lock / tmp / .DS_Store 等临时目录被误判为新会话。
        return {p.name for p in self.session_root.iterdir()
                if p.is_dir() and p.name.startswith("sess_")}

    def wait_new_session(self, before: set, timeout_s: float = 90.0) -> Optional[Path]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            new = self.snapshot_sessions() - before
            if new:
                newest = max(
                    (self.session_root / n for n in new),
                    key=lambda p: p.stat().st_mtime,
                )
                return newest
            time.sleep(0.8)
        return None

    def wait_settled(self, sess_dir: Path, timeout_s: float = 240.0,
                     quiet_s: float = 3.0) -> bool:
        """等待会话日志落盘稳定（连续 quiet_s 秒无变化且结构完整）。

        每个轮询周期先做一次自动确认检查 —— 计划确认卡片出现时回合已
        completedAt，若先判稳会把「计划书」当最终答复收取（假通过）。
        判稳前若点到确认，视为会话重新活动，重置静默计时。
        """
        deadline = time.time() + timeout_s
        last_sig, last_change = None, time.time()
        while time.time() < deadline:
            if self._maybe_auto_confirm() is not None:
                last_change = time.time()      # 确认后 agent 继续工作，重置静默
            msg_file = sess_dir / "session.messages.json"
            sig = None
            if msg_file.is_file():
                try:
                    st = msg_file.stat()
                    sig = (st.st_size, round(st.st_mtime, 3))
                except OSError:
                    sig = None
            if sig != last_sig:
                last_sig, last_change = sig, time.time()
            else:
                if sig and (time.time() - last_change) >= quiet_s and self._looks_complete(msg_file):
                    return True
            time.sleep(0.5)
        return False

    def wait_some_settled(self, sessions: list, timeout_s: float = 240.0,
                          quiet_s: float = 3.0, inflight_pairs=None,
                          cycle_s: float = 0.0) -> list:
        """流水线模式：轮询多条在途会话，阻塞到【至少一条】稳定（或超时）。

        返回本次已稳定的会话目录列表（空列表 = 等满 timeout_s 一条都没稳定）。
        判稳口径与 wait_settled 相同：签名（size+mtime）quiet_s 秒不变
        且 _looks_complete 通过。
        每个轮询周期先做自动确认检查（先于判稳，防止把计划确认当完成收取）。
        inflight_pairs/cycle_s：传入 [(sess, res)] 与巡检间隔后，每 cycle_s 秒
        依次切换视图到各在途会话扫描确认卡片（卡片只渲染在可见会话，见
        cycle_and_confirm）。不传则维持旧行为（只扫当前可见视图）。
        """
        deadline = time.time() + timeout_s
        sigs: dict = {}
        last_change = {s: time.time() for s in sessions}
        remaining = list(sessions)
        settled: list = []
        next_cycle = time.time() + cycle_s
        while time.time() < deadline and remaining:
            # 视图巡检：节流到 cycle_s，命中点击后重置全部静默计时
            if (inflight_pairs and cycle_s > 0 and not self.view_cycle_disabled
                    and time.time() >= next_cycle):
                try:
                    if self.cycle_and_confirm(inflight_pairs, set(remaining)) > 0:
                        for s in last_change:
                            last_change[s] = time.time()
                except Exception as exc:
                    # 巡检本身出错不该打断等待，但也不能静默吞掉 —— 只说「跳过了本轮」
                    cite_emit(f"[确认] 本轮巡检出错已跳过：{type(exc).__name__}")
                next_cycle = time.time() + cycle_s
            if self._maybe_auto_confirm() is not None:
                # 点了确认 → 所有在途会话的静默计时重置
                for s in last_change:
                    last_change[s] = time.time()
            now = time.time()
            for s in list(remaining):
                mf = s / "session.messages.json"
                sig = None
                if mf.is_file():
                    try:
                        st = mf.stat()
                        sig = (st.st_size, round(st.st_mtime, 3))
                    except OSError:
                        sig = None
                if sig != sigs.get(s):
                    sigs[s] = sig
                    last_change[s] = now
                elif sig and (now - last_change[s]) >= quiet_s and self._looks_complete(mf):
                    settled.append(s)
                    remaining.remove(s)
                    sigs.pop(s, None)
            if settled:
                return settled          # 谁先完成先收谁
            time.sleep(0.5)
        return settled

    @staticmethod
    def _looks_complete(msg_file: Path) -> bool:
        try:
            data = json.loads(msg_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        msgs = data.get("messages") or []
        if len(msgs) < 2:
            return False
        last = msgs[-1]
        if last.get("role") != "assistant":
            return False
        if last.get("isStreaming"):
            return False
        return bool((last.get("content") or "").strip() or last.get("completedAt"))
