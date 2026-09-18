#!/usr/bin/env python3
"""测试平台可视化界面 · 本地服务。

纯标准库实现（零额外依赖），职责：
  * 一键运行：后台子进程执行 run_pipeline.py，实时回收 stdout
  * 轮次归档：读取用户工作区 rounds/<run_id>/ 的落盘数据
  * REST API：轮次列表 / 轮次详情 / 运行状态 / 预设用例 / Finder 定位

    python server.py [--port 8765]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

from core.llm_client import (clear_config, load_config, probe_provider,
                             public_config, save_config)
from core.settings import (clear_overrides, config_path, describe,
                           effective_config, preset_path, rounds_dir,
                           save_overrides, uploads_dir, user_workspace)

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
# 轮次归档与平台配置都在用户工作区（历史位置自动迁移，重启后生效）
ROUNDS = rounds_dir()

MAX_LOG_LINES = 800

CONFIG_PATH = config_path()

# --allow-lan 临时放行局域网访问（默认关）。见 main() 与 _same_origin_ok()。
ALLOW_LAN = False


def _is_private_host(host: str) -> bool:
    """私网地址判断：127.x / 10.x / 172.16-31.x / 192.168.x / localhost / ::1。

    主机名（非 IP 字面量）一律不算私网 —— 放行它等于向 DNS 重绑定开门。
    """
    h = (host or "").strip("[]").lower()
    if h in ("localhost", "::1") or h.startswith("127."):
        return True
    m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", h)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
    return False

# ---------------------------------------------------------------- 附件库
# 被测应用的「引用本地文件」本质是**绝对路径列表**（前端取 Electron File.path），
# 因此平台上传的附件必须真落盘到本机某个绝对路径上，投递时把该路径拖给应用。
# 附件库存放在用户工作区（core.settings.uploads_dir），与代码分离、可直接查看。
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """附件名净化：只取基名并去掉危险字符（防目录穿越）。"""
    base = Path(str(name or "")).name
    base = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", base).strip(" .")
    return base or "attachment"


def _unique_upload_dir() -> Path:
    """每个附件一个独立目录（按时间命名，冲突时加序号）：保留原文件名又不互相覆盖。"""
    base = time.strftime("%Y%m%d_%H%M%S")
    root = uploads_dir()
    d = root / base
    i = 1
    while d.exists():
        i += 1
        d = root / f"{base}_{i}"
    return d


def list_uploads() -> list:
    """附件库清单（按修改时间倒序），供界面行内直接复用已上传的附件。"""
    out = []
    root = uploads_dir()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "path": str(p.resolve()), "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:200]


def save_upload(name: str, data: bytes) -> Path:
    d = _unique_upload_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe_filename(name)
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------- 环境档案
def read_env_config() -> dict:
    """读取可选的运行环境档案，以及当前默认项。

    返回 {current, profiles:[{key,label,desc}], app_running}
    环境在应用【启动时】注入，运行中的实例无法改环境，
    因此前端在应用运行时会把选择器置为只读。
    """
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    app = cfg.get("app") or {}
    current = str(app.get("env_profile") or "")
    profiles_cfg = app.get("env_profiles") or {}

    labels = {
        "dev":        ("开发环境", "注入 dev 域名，连 api-dev.3ren.cn"),
        "production": ("线上环境", "不注入变量，走应用内置生产域名"),
    }
    profiles = []
    for key in ("dev", "production"):
        if key not in profiles_cfg:
            continue
        label, desc = labels.get(key, (key, f"注入 {len(profiles_cfg.get(key) or {})} 个变量"))
        profiles.append({"key": key, "label": label, "desc": desc})
    # 允许配置里出现自定义档案
    for key, pairs in profiles_cfg.items():
        if key in ("dev", "production"):
            continue
        profiles.append({"key": key, "label": key,
                         "desc": f"自定义档案（{len(pairs or {})} 个变量）"})
    return {
        "current": current,
        "none_label": "不注入（遵循应用内置默认）",
        "profiles": profiles,
        "app_running": app_is_running(cfg),
    }


def write_env_profile(profile: str) -> tuple[bool, str]:
    """把选定的环境档案写回 config.yaml 的 app.env_profile。

    只改这一行，其余内容保持原样（用文本替换而非 yaml.dump，
    避免注释与格式被整体重写）。
    """
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    app = cfg.get("app") or {}
    valid = set((app.get("env_profiles") or {}).keys()) | {""}
    key = str(profile or "").strip()
    if key not in valid:
        return False, f"未知环境档案：{key or '(空)'}"

    text = CONFIG_PATH.read_text(encoding="utf-8")
    new_text, n = re.subn(r"(?m)^(\s*)env_profile:.*$",
                          lambda m: f'{m.group(1)}env_profile: "{key}"',
                          text, count=1)
    if n == 0:
        return False, "config.yaml 中未找到 env_profile 字段"
    try:
        CONFIG_PATH.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        return False, f"写入失败：{type(exc).__name__}: {exc}"
    return True, key


def load_cfg() -> dict:
    """读取 config.yaml 并做三层合并（.app_settings.json > config.yaml > 内置默认）。

    应用路径 / 日志路径相关的消费方（kill_app / 子进程流水线 / 探查脚本）
    一律经由这里取值，保证界面「应用设置」的覆盖项全局生效。
    """
    return effective_config(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})


def app_is_running(cfg: dict | None = None) -> bool:
    """应用是否在运行：以调试端口是否响应为准（与驱动 is_up 同口径）。"""
    try:
        cfg = cfg or (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})
        port = int((cfg.get("app") or {}).get("debug_port") or 9222)
    except Exception:
        port = 9222
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            json.load(r)
        return True
    except Exception:
        return False


def close_app() -> tuple[bool, str]:
    """关闭应用实例 —— 不区分是否由平台启动。

    服务端以子进程方式跑测试，本身不持有驱动对象，
    因此直接按可执行文件路径匹配系统进程来关闭：
    这样既覆盖平台启动的实例，也覆盖用户从 Dock 手动启动的实例。
    """
    try:
        cfg = load_cfg()
        binary = str((cfg.get("app") or {}).get("binary") or "").strip()
    except Exception as exc:
        return False, f"读取配置失败：{type(exc).__name__}: {exc}"
    if not binary:
        return False, "config.yaml 未配置 app.binary，无法定位进程"

    # -x 已精确匹配 executable 名（命令行首 token），不再用 -f 路径字串匹配，
    # 避免同 binary 路径字串命中其它无关进程（Electron / node / python 等常见名）。
    exe_name = Path(binary).name
    if not exe_name:
        return False, "binary 配置无法解析为可执行文件名"
    try:
        if sys.platform == "win32":
            r = subprocess.run(["taskkill", "/F", "/IM", exe_name], capture_output=True, timeout=10)
        else:
            r = subprocess.run(["pkill", "-x", exe_name], capture_output=True, timeout=10)
    except Exception as exc:
        return False, f"关闭失败：{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return False, "未找到运行中的应用进程"

    # 等端口释放，让用户点完就有明确反馈
    for _ in range(20):
        if not app_is_running():
            return True, "应用已关闭"
        time.sleep(0.5)
    return True, "已发出关闭信号（端口未及时释放，可能仍在退出中）"


class RunState:
    """同一时刻只允许一轮运行。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.run_id: str = ""
        self.started_at: float = 0.0
        self.lines: deque = deque(maxlen=MAX_LOG_LINES)
        self.exit_code: int | None = None
        self.console_path: Path | None = None
        self.stopped: bool = False

    # ------------------------------------------------ 运行控制
    def start(self, cases: int, repro_times: int, repro_limit: int,
              case_items: list | None = None,
              max_inflight: int = 0) -> tuple[bool, str]:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False, "已有测试正在运行，请等待完成"
            self.run_id = time.strftime("run_%Y%m%d_%H%M%S")
            self.started_at = time.time()
            self.lines.clear()
            self.exit_code = None
            self.stopped = False

            run_dir = ROUNDS / self.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            self.console_path = run_dir / "console.log"

            cmd = [sys.executable, str(ROOT / "run_pipeline.py"),
                   "--run-id", self.run_id]
            # 界面手动输入的用例落成本轮专属文件，与预设用例完全隔离
            if case_items:
                cf = run_dir / "cases.yaml"
                cf.write_text(
                    yaml.safe_dump({"cases": case_items}, allow_unicode=True,
                                   sort_keys=False),
                    encoding="utf-8")
                cmd += ["--cases-file", str(cf)]
            if cases:
                cmd += ["--cases", str(int(cases))]
            if repro_times:
                cmd += ["--repro-times", str(int(repro_times))]
                if repro_limit:
                    cmd += ["--repro-limit", str(int(repro_limit))]
            if max_inflight:
                cmd += ["--max-inflight", str(int(max_inflight))]

            try:
                # start_new_session：让测试进程独立于服务所在进程组，
                # 避免宿主环境回收进程树时把正在运行的测试连带杀掉（与应用启动同策略）。
                # stdin=DEVNULL：服务以独立进程组启动后继承的 stdin 是坏 fd，
                # 子进程会因 init_sys_streams Bad file descriptor 起不来。
                kwargs = {}
                if sys.platform == "win32":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
                else:
                    kwargs["start_new_session"] = True

                self.proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    stdin=subprocess.DEVNULL, **kwargs
                )
            except Exception as exc:
                return False, f"启动失败: {exc}"

            threading.Thread(target=self._pump, daemon=True).start()
            self._line(f"[平台] 测试已启动 · run_id={self.run_id}")
            self._line(f"[平台] 命令: {' '.join(cmd[2:])}")
            return True, self.run_id

    def stop(self) -> tuple[bool, str]:
        """手动终止正在运行的测试（只杀测试进程，不影响常驻应用）。"""
        with self.lock:
            if not (self.proc and self.proc.poll() is None):
                return False, "当前没有正在运行的测试"
            proc = self.proc
            self.stopped = True
        try:
            proc.terminate()                     # SIGTERM → 进程退出，本轮不归档
        except Exception as exc:
            return False, f"终止失败: {type(exc).__name__}: {exc}"
        # 3 秒未退出则强杀（后台执行，不阻塞 HTTP 响应）
        def _hard_kill():
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        threading.Thread(target=_hard_kill, daemon=True).start()
        self._line("[平台] 已收到手动终止指令，正在停止测试进程…")
        return True, "已发送终止信号"

    def _pump(self):
        proc = self.proc
        assert proc and proc.stdout
        try:
            with self.console_path.open("w", encoding="utf-8") as logf:
                for line in proc.stdout:
                    self._line(line.rstrip())
                    try:
                        logf.write(line)
                        logf.flush()
                    except Exception:
                        pass
        finally:
            # 任何路径下都必须 wait：否则 exit_code 永久 None，
            # 前端 renderStatus 会把异常退出误显示为「空闲」。
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        with self.lock:
            self.exit_code = proc.returncode
        ok = (ROUNDS / self.run_id / "round_summary.json").is_file()
        self._line(f"[平台] 进程退出 code={proc.returncode}"
                   + ("，轮次数据已归档 ✓" if ok else "，⚠ 未产出轮次归档（可能中途失败）"))

    def _line(self, text: str):
        self.lines.append(f"{time.strftime('%H:%M:%S')}  {text}")

    # ------------------------------------------------ 状态
    def status(self) -> dict:
        with self.lock:
            running = bool(self.proc and self.proc.poll() is None)
            done_ok = False
            if self.run_id and not running:
                done_ok = (ROUNDS / self.run_id / "round_summary.json").is_file()
            # 归档完成度：进程正常退出（code=0）即视为本轮成功结束。
            # 单独依赖 archived 会有竞态 —— 子进程退出与 round_summary.json
            # 落盘之间存在毫秒级窗口，前端恰好在这期间轮询就会读到
            # archived=False，从而把「完成」误显示为「退出码 0」。
            finished_ok = (self.exit_code == 0)
            return {
                "running": running,
                "run_id": self.run_id,
                "started_at": self.started_at,
                "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
                "exit_code": self.exit_code,
                "archived": done_ok,
                "finished_ok": finished_ok,
                "stopped": self.stopped,
                "lines": list(self.lines)[-90:],
            }


STATE = RunState()


# ---------------------------------------------------------------- 评估任务
class EvalState:
    """质量评估后台任务：同一时刻只允许一个评估在跑（与 RunState 同构）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.run_id = ""
        self.started_at = 0.0
        self.lines: deque = deque(maxlen=MAX_LOG_LINES)
        self.done = 0
        self.total = 0
        self.error = ""
        self.finished = False
        self.stopped = False
        self.summary: dict = {}
        # 最近一次启动前探活的结构化结果（含供应商原文与来源 IP），供前端展示
        self.last_preflight: dict = {}

    def _preflight(self) -> dict:
        """用服务端已保存的配置真实调一次模型。

        必须在判分前做：若 Key / 来源 IP / 模型名有问题，逐用例判分就是
        「发 N 次注定失败的请求」—— 既烧额度，又要等 N 次超时。
        """
        try:
            from core.evaluator import preflight_check
            return preflight_check({"llm": load_config()})
        except Exception as exc:
            return {"ok": False,
                    "message": f"启动前校验异常：{type(exc).__name__}: {exc}"}

    def start(self, run_id: str, max_cases: int = 0, concurrency: int = 3) -> tuple:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False, "已有评估任务在运行，请等待完成"
            if not (ROUNDS / run_id / "round_detail.json").is_file():
                return False, f"轮次不存在或缺少 round_detail.json：{run_id}"

        # 探活在锁外执行（最长约 12s），避免阻塞停止/状态查询。
        # 不通过 → 直接拒绝启动，且不写 evaluation.json（不覆盖上一份有效结果）。
        self.last_preflight = self._preflight()
        if not self.last_preflight.get("ok"):
            msg = str(self.last_preflight.get("message") or "模型不可用，已中止评估")
            self._line(f"[评估] 已拒绝启动：{msg}")
            return False, f"模型不可用，未开始评估：{msg}"

        with self.lock:
            if self.thread and self.thread.is_alive():
                return False, "已有评估任务在运行，请等待完成"
            self.run_id = run_id
            self.started_at = time.time()
            self.lines.clear()
            self.done = self.total = 0
            self.error = ""
            self.finished = False
            self.stopped = False
            self.summary = {}
            self.thread = threading.Thread(
                target=self._run, args=(run_id, max_cases, concurrency), daemon=True)
            self.thread.start()
        self._line(f"[评估] 已启动 · run_id={run_id}")
        return True, run_id

    def _progress(self, done: int, total: int, message: str):
        self.done, self.total = done, total
        self._line(message)

    def _run(self, run_id: str, max_cases: int, concurrency: int):
        try:
            from core.evaluator import evaluate_round
            res = evaluate_round(
                run_id, max_cases=max_cases,
                cfg={"llm": {"eval_concurrency": concurrency}},
                on_progress=self._progress,
                cancel=lambda: self.stopped,
                # 启动前已在 start() 探活通过，此处不再重复发一次请求
                preflight=False,
            )
            if res.get("aborted"):
                self.error = str(res.get("error") or "模型不可用，已中止评估")
                self._line(f"[评估] 已中止：{self.error}")
                return
            self.summary = res.get("summary") or {}
            if res.get("error"):
                self.error = str(res["error"])
            self._line(f"[评估] 完成：用例 {len(res.get('cases') or [])} 条，"
                       f"综合分 {self.summary.get('overall_score_100')}")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._line(f"[评估] 失败：{self.error}")
        finally:
            self.finished = True

    def stop(self) -> tuple:
        with self.lock:
            if not (self.thread and self.thread.is_alive()):
                return False, "当前没有正在运行的评估"
            self.stopped = True
        self._line("[评估] 已请求停止（当前用例完成后退出）")
        return True, "已请求停止"

    def _line(self, text: str):
        self.lines.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def status(self) -> dict:
        running = bool(self.thread and self.thread.is_alive())
        return {
            "running": running,
            "run_id": self.run_id,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "finished": self.finished,
            "stopped": self.stopped,
            "summary": self.summary,
            "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "lines": list(self.lines)[-60:],
        }


EVAL = EvalState()


# ---------------------------------------------------------------- 轮次读取
# ---------------------------------------------------------------- 用例
def normalize_cases(raw) -> list:
    """把界面传来的用例整理成 pipeline 可消费的结构。

    界面只填 prompt（必填）；id 与 name 一律自动生成
    （CASE-001… / 用例 1…），避免「填了看不见」的冗余输入项。
    从 YAML 预设导入时若带 name，则保留。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            item = {"prompt": item}
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        out.append({
            "id": str(item.get("id") or f"CASE-{i:03d}"),
            "name": str(item.get("name") or f"用例 {i}"),
            "prompt": prompt,
            "expect_tools": item.get("expect_tools") or [],
            # 预设用例的三维度标签（scene/targets/attachment），供界面
            # 「导入预设」弹窗按标签筛选，也是评估判定场景/产物要求的依据。
            "labels": item.get("labels") or {},
            # 用例附件（本机绝对路径）：运行时由驱动拖拽投递给应用
            "attachments": [str(p) for p in (item.get("attachments") or []) if str(p).strip()],
        })
    return out


def preset_cases() -> list:
    """读取用户工作区的预设用例，供界面「导入预设」使用。"""
    f = preset_path()
    if not f.is_file():
        return []
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return normalize_cases(data.get("cases") or [])


# ---------------------------------------------------------------- 预设用例管理
# 用户可在界面直接新增 / 删除预设用例（写回工作区 testcases.yaml）。
# 头部注释（cases: 之前的全部内容）在写回时原样保留，只重写用例清单本体。


def _load_preset_raw() -> list:
    """读取预设用例的原始 dict 列表（未经 normalize，保留全部字段）。"""
    f = preset_path()
    if not f.is_file():
        return []
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    return [c for c in (data.get("cases") or []) if isinstance(c, dict)]


def _save_preset_raw(cases: list) -> None:
    """原子写回 testcases.yaml。

    文件头部注释（首个 cases: 行之前）原样保留 —— 头部记录了标签口径说明，
    丢掉会让预设标签变成无解释的裸数据；用例清单本体用 yaml 重写
    （逐条追加式文本改写在千条规模下不可靠）。
    """
    f = preset_path()
    head = ""
    if f.is_file():
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^cases:\s*$", text, flags=re.M)
        if m:
            head = text[: m.start()]
    body = yaml.safe_dump({"cases": cases}, allow_unicode=True, sort_keys=False,
                          default_flow_style=False,
                          width=4096)   # 足够宽：prompt 不折行，未触碰条目保持字节级原样
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(head + body, encoding="utf-8")
    os.replace(tmp, f)


def _norm_label_input(payload: dict) -> dict:
    """把界面传来的标签整理成 testcases.yaml 的三维度口径，空值不落盘。"""
    labels = payload.get("labels") or {}
    out: dict = {}
    scene = str(labels.get("scene") or "").strip()
    if scene:
        out["scene"] = scene
    targets = [str(t).strip() for t in (labels.get("targets") or []) if str(t).strip()]
    if targets:
        out["targets"] = targets
    if labels.get("attachment") is True:
        out["attachment"] = True
    return out


def add_preset_case(payload: dict) -> tuple[bool, str, dict | None]:
    """新增一条预设用例：id 取现有最大 CASE-N 顺延，保证唯一。"""
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return False, "提示词（prompt）为必填", None
    cases = _load_preset_raw()
    n = 0
    for c in cases:
        m = re.match(r"^CASE-(\d+)$", str(c.get("id") or ""))
        if m:
            n = max(n, int(m.group(1)))
    case = {
        "id": f"CASE-{n + 1:03d}",
        "name": str(payload.get("name") or "").strip() or f"自定义-{n + 1:03d}",
        "prompt": prompt,
        "expect_tools": [str(t).strip() for t in (payload.get("expect_tools") or [])
                         if str(t).strip()],
        "labels": _norm_label_input(payload),
    }
    cases.append(case)
    _save_preset_raw(cases)
    return True, f"已新增 {case['id']}（预设共 {len(cases)} 条）", case


def delete_preset_cases(ids) -> tuple[bool, str, int]:
    """按 id 批量删除预设用例；未命中任何 id 时报错而非静默成功。"""
    idset = {str(i).strip() for i in (ids or []) if str(i).strip()}
    if not idset:
        return False, "未指定要删除的用例 id", 0
    cases = _load_preset_raw()
    kept = [c for c in cases if str(c.get("id") or "") not in idset]
    removed = len(cases) - len(kept)
    if removed == 0:
        return False, "没有匹配到要删除的预设用例（可能已被删除）", 0
    _save_preset_raw(kept)
    return True, f"已删除 {removed} 条（预设剩余 {len(kept)} 条）", removed


def reveal_in_finder(target: str) -> tuple[bool, str]:
    """在文件管理器中定位目标文件/目录（跨平台）。

    为什么需要后端代劳：浏览器禁止 http:// 页面跳转到 file://
    （Not allowed to load local resource），因此无法从前端直接打开本机文件。
    这里由本地服务代劳：
      - macOS：调用 `open -R`，交给 Finder 选中并弹窗
      - Windows：调用 `explorer /select,<path>`，打开资源管理器并选中

    目标不存在则退回打开其父目录。
    """
    p = Path(target).expanduser()
    if p.is_file():
        path_arg = str(p)
    elif p.is_dir():
        path_arg = str(p)
    else:
        parent = p.parent
        if not parent.is_dir():
            return False, f"路径不存在：{target}"
        path_arg = str(parent)
    try:
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", path_arg], check=True,
                           capture_output=True, timeout=10)
        else:
            subprocess.run(["open", "-R", path_arg], check=True,
                           capture_output=True, timeout=10)
        return True, path_arg
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or b"").decode("utf-8", "replace").strip()
        return False, f"文件管理器调用失败：{msg or exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def delete_round(run_id: str) -> tuple[bool, str]:
    """删除一个轮次归档目录（工作区 rounds/<run_id>/）。

    安全约束：
      * run_id 必须通过字符白名单校验，杜绝 ../ 之类的路径穿越；
      * 目标必须真实位于 ROUNDS 目录内；
      * 正在运行的轮次不允许删除。
    """
    rid = str(run_id or "").strip()
    if not rid:
        return False, "缺少 run_id"
    # 只允许字母/数字/下划线/连字符：既挡住 ../，也挡住绝对路径
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rid):
        return False, f"run_id 含非法字符：{rid}"
    if STATE.run_id == rid and STATE.proc and STATE.proc.poll() is None:
        return False, "该轮次正在运行，无法删除"

    target = (ROUNDS / rid).resolve()
    root = ROUNDS.resolve()
    if target == root or root not in target.parents:
        return False, f"目标不在轮次目录内：{rid}"
    if not target.is_dir():
        return False, f"轮次不存在：{rid}"

    try:
        shutil.rmtree(target)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    # 若删的是当前查看的轮次，清掉缓存的运行状态指向
    if STATE.run_id == rid:
        STATE.run_id = ""
    return True, rid


def list_rounds() -> list:
    out = []
    if not ROUNDS.is_dir():
        return out
    for d in sorted(ROUNDS.iterdir(), reverse=True):
        f = d / "round_summary.json"
        if not d.is_dir() or not f.is_file():
            continue
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            # 只回传列表预览必需的字段：case_id / name / prompt / status
            cases = [
                {
                    "case_id": c.get("case_id", ""),
                    "name": c.get("name", ""),
                    "prompt": c.get("prompt", ""),
                    "status": c.get("status", ""),
                }
                for c in (s.get("cases") or [])
            ]
            out.append({
                # run_id 一律取**目录名**：路由 /api/rounds/<rid> 是按目录名解析的，
                # 若这里用 JSON 内的 run_id，两者不一致时列表点开的是另一个轮次（或 404）。
                "run_id": d.name,
                "finished_at": s.get("finished_at", ""),
                "run_mode": s.get("run_mode", ""),
                "elapsed_s": s.get("elapsed_s", 0),
                "app_version": s.get("app_version", ""),
                "summary": s.get("summary") or {},
                "repro_summary": s.get("repro_summary") or {},
                "round_skills": s.get("round_skills") or [],
                "has_evaluation": (d / "evaluation.json").is_file(),
                "cases": cases,
            })
        except Exception:
            continue
    return out


def load_round(run_id: str) -> dict | None:
    d = ROUNDS / run_id
    if not d.is_dir():
        return None
    detail = summary = evaluation = None
    fd, fs, fe = d / "round_detail.json", d / "round_summary.json", d / "evaluation.json"
    try:
        if fd.is_file():
            detail = json.loads(fd.read_text(encoding="utf-8"))
        if fs.is_file():
            summary = json.loads(fs.read_text(encoding="utf-8"))
        if fe.is_file():
            evaluation = json.loads(fe.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {"run_id": run_id, "summary": summary, "detail": detail,
            "evaluation": evaluation}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):   # 静默访问日志
        pass

    # ------------------------------------------------------------ 工具
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            f = WEB / "index.html"
            if f.is_file():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, "dashboard 未找到".encode(), "text/plain; charset=utf-8")
            return

        if path == "/api/rounds":
            # dir：轮次归档根目录的绝对路径，前端据此拼出各轮次文件夹的访达链接
            self._json({"rounds": list_rounds(), "dir": str(ROUNDS)})
            return

        if path.startswith("/api/rounds/"):
            rid = path.split("/")[3] if len(path.split("/")) > 3 else ""
            if path.endswith("/report.html"):
                f = ROUNDS / rid / "report.html"
                if f.is_file():
                    self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")
                return
            data = load_round(rid)
            self._json(data or {"error": "round not found"}, 200 if data else 404)
            return

        if path == "/api/run/status":
            self._json(STATE.status())
            return

        if path == "/api/config":
            try:
                cfg = yaml.safe_load(config_path().read_text(encoding="utf-8"))
                self._json({
                    "app_name": (cfg.get("app") or {}).get("name", ""),
                    "case_timeout_s": cfg.get("case_timeout_s", 1200),
                    "preset_count": len(preset_cases()),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return

        # 预设用例（仅作为「导入」来源，不作为默认执行内容）
        if path == "/api/preset-cases":
            self._json({"cases": preset_cases()})
            return

        # 可选环境档案 + 应用运行状态（前端据此做只读/可编辑互斥）
        if path == "/api/env":
            try:
                self._json(read_env_config())
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if path == "/api/app/status":
            self._json({"running": app_is_running()})
            return

        # 应用设置：被测应用路径 / 日志根目录（含来源与存在性，供界面回显）
        if path == "/api/app-settings":
            try:
                self._json(describe(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        # 后端模型配置（脱敏，绝不返回明文 Key）
        if path == "/api/llm/config":
            self._json(public_config())
            return

        # 附件库：已上传附件清单（用例行内可直接复用）
        if path == "/api/uploads":
            self._json({"files": list_uploads()})
            return

        # 质量评估任务状态（进度轮询 + 日志回放）
        if path == "/api/eval/status":
            self._json(EVAL.status())
            return
        self._send(404, b"not found", "text/plain")

    def _same_origin_ok(self) -> bool:
        """同源校验：本服务可关应用、跑测试、代发外网请求，
        必须挡掉恶意页面驱动的 CSRF 与 DNS 重绑定。
        Host 非本机，或带 Origin 但不是本机 → 一律拒绝。
        --allow-lan 临时放行时，额外放行私网 IP（10/172.16-31/192.168），
        公网 IP 与陌生域名仍拒绝，缩小被恶意页面利用的面。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        ok_hosts = ("127.0.0.1", "localhost", "::1")
        if host not in ok_hosts and not (ALLOW_LAN and _is_private_host(host)):
            return False
        origin = self.headers.get("Origin")
        if origin:
            oh = urlparse(origin).hostname or ""
            if oh not in ok_hosts and not (ALLOW_LAN and _is_private_host(oh)):
                return False
        return True

    def do_POST(self):
        if not self._same_origin_ok():
            # 返回 JSON（而非纯文本）：前端 r.json() 才能解析出可读原因。
            # 同时回显观测到的 Host/Origin，便于定位是哪种访问方式被拦。
            self._json({
                "ok": False,
                "category": "forbidden",
                "message": ("来源校验未通过：仅允许经 127.0.0.1 / localhost 访问本服务"
                            f"（Host={self.headers.get('Host') or '-'}，"
                            f"Origin={self.headers.get('Origin') or '-'}）"),
            }, 403)
            return
        path = urlparse(self.path).path

        # 切换环境：写入 config.yaml（下次启动应用时生效）
        if path == "/api/env":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            if app_is_running():
                self._json({"ok": False,
                            "message": "应用正在运行，环境为只读。请先关闭实例再切换。"}, 409)
                return
            ok, msg = write_env_profile(body.get("profile", ""))
            data = read_env_config() if ok else {}
            self._json({"ok": ok, "message": msg, **(data if ok else {})},
                       200 if ok else 400)
            return

        # 关闭应用实例
        if path == "/api/run/stop":
            ok, msg = STATE.stop()
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            return

        if path == "/api/app/close":
            if STATE.proc and STATE.proc.poll() is None:
                self._json({"ok": False, "message": "有测试正在运行，无法关闭应用"}, 409)
                return
            ok, msg = close_app()
            self._json({"ok": ok, "message": msg,
                        "running": app_is_running()}, 200 if ok else 400)
            return

        if path == "/api/run":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            ok, msg = STATE.start(
                cases=body.get("cases", 0),
                repro_times=body.get("repro_times", 0),
                repro_limit=body.get("repro_limit", 0),
                case_items=normalize_cases(body.get("case_items")),
                max_inflight=int(body.get("max_inflight", 0) or 0),
            )
            self._json({"ok": ok, "run_id": msg if ok else "", "message": msg},
                       200 if ok else 409)
            return

        # 后端模型配置：写入独立密钥文件（0600、不进版本库）；响应只含脱敏信息
        if path == "/api/llm/config":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            if body.get("clear"):
                clear_config()
                self._json({"ok": True, "message": "已清空后端模型配置", **public_config()})
                return
            # 回显：把服务端已保存的 Key 返回给本机界面（默认隐藏，仅显式请求时返回）。
            # 平台是单用户本机服务，Key 本就存在用户自己的工作区文件里；
            # 用 POST 显式触发而非 GET 默认携带，避免 Key 随普通查询到处流转。
            if body.get("reveal"):
                self._json({"ok": True, "api_key": load_config().get("api_key") or ""})
                return
            base_url = str(body.get("base_url") or "").strip()
            api_key = str(body.get("api_key") or "").strip()
            if not api_key and body.get("keep_key"):
                # 沿用已保存的 Key（用于只改地址/模型名），不回显给前端
                api_key = load_config().get("api_key") or ""
            model = str(body.get("model") or "").strip()
            if not base_url or not api_key:
                self._json({"ok": False, "message": "供应商地址与 API Key 均为必填"}, 400)
                return
            if not model:
                # 模型名必填：留空会让判分请求带着占位模型名发出去，
                # 换回一句与真实原因无关的报错（TokenHub 等要求填模型名 / 端点 ID）。
                self._json({"ok": False,
                            "message": "模型名为必填：请填写模型名或端点 ID"
                                       "（留空会导致判分请求使用占位模型名而失败）"}, 400)
                return
            save_config(base_url, api_key, model)
            self._json({"ok": True, "message": "已保存到服务端", **public_config()})
            return

        # 预设用例管理：新增 / 删除（直接写 testcases.yaml，保留文件头注释）
        if path == "/api/preset-cases":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            action = str(body.get("action") or "").strip()
            if action == "add":
                ok, msg, case = add_preset_case(body)
                self._json({"ok": ok, "message": msg, "case": case},
                           200 if ok else 400)
                return
            if action == "delete":
                ok, msg, removed = delete_preset_cases(body.get("ids") or [])
                self._json({"ok": ok, "message": msg, "removed": removed},
                           200 if ok else 400)
                return
            self._json({"ok": False, "message": "未知操作（支持 add / delete）"}, 400)
            return

        # 应用设置：保存被测应用路径 / 日志根目录（写 .app_settings.json，0600）。
        # 两项都留空 = 清除覆盖文件，完全回退 config.yaml / 内置默认。
        if path == "/api/app-settings":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            if body.get("reset"):
                clear_overrides()
                self._json({"ok": True, "message": "已恢复默认配置",
                            **describe(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})})
                return
            binary = str(body.get("app_binary") or "").strip()
            session_root = str(body.get("session_root") or "").strip()
            user_ws = str(body.get("user_workspace") or "").strip()
            for label, v in (("应用路径", binary), ("日志目录", session_root),
                             ("工作区目录", user_ws)):
                if v and v.startswith(("|", ";")):
                    self._json({"ok": False, "message": f"{label}含非法字符"}, 400)
                    return
            if binary and not Path(binary).expanduser().exists():
                self._json({"ok": False,
                            "message": f"应用路径不存在：{binary}（请确认 .app/Contents/MacOS/ 下的可执行文件）"},
                           400)
                return
            if user_ws and not Path(user_ws).expanduser().parent.is_dir():
                self._json({"ok": False,
                            "message": f"工作区目录的父目录不存在：{user_ws}"},
                           400)
                return
            save_overrides(binary, session_root, user_ws)
            if user_ws:
                user_workspace()   # 立即建目录，让「打开工作区」随时可用
            src = describe(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})
            self._json({"ok": True, "message": "已保存（下次运行测试时生效）", **src})
            return

        # 启动质量评估（后台任务，用 GET /api/eval/status 轮询进度）
        if path == "/api/evaluate":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            run_id = str(body.get("run_id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                self._json({"ok": False, "message": f"run_id 含非法字符：{run_id}"}, 400)
                return
            ok, msg = EVAL.start(run_id,
                                 max_cases=int(body.get("max_cases") or 0),
                                 concurrency=int(body.get("concurrency") or 3))
            # preflight：启动前探活的结构化结果（含供应商原文与来源 IP）。
            # 失败时前端据此直接展示可操作根因，而不是干等一条「评估失败」。
            self._json({"ok": ok, "run_id": msg if ok else "", "message": msg,
                        "preflight": EVAL.last_preflight or None},
                       200 if ok else 409)
            return

        if path == "/api/evaluate/stop":
            ok, msg = EVAL.stop()
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            return

        # 大模型供应商探活：Key 由前端随请求传入，用后即弃、不落盘
        if path == "/api/llm/test":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            try:
                timeout_s = float(body.get("timeout_s") or 12.0)
            except (TypeError, ValueError):
                timeout_s = 12.0
            result = probe_provider(
                str(body.get("base_url") or ""),
                str(body.get("api_key") or ""),
                model=str(body.get("model") or ""),
                probe_path=str(body.get("probe_path") or ""),
                timeout_s=timeout_s,
            )
            # 探活失败也是「有效结果」，仍以 HTTP 200 返回，由 ok 字段表达
            self._json(result.to_dict())
            return

        # 附件上传：浏览器选中的文件经本地服务落盘，返回**绝对路径** ——
        # 被测应用的「引用本地文件」本质就是绝对路径列表，必须真落盘才能被投递。
        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            name = str(body.get("name") or "").strip()
            b64 = str(body.get("data_base64") or "")
            if not name or not b64:
                self._json({"ok": False, "message": "缺少文件名或文件内容"}, 400)
                return
            try:
                raw = base64.b64decode(b64)
            except Exception as exc:
                self._json({"ok": False, "message": f"内容不是合法 base64：{exc}"}, 400)
                return
            if len(raw) > MAX_UPLOAD_BYTES:
                self._json({"ok": False,
                            "message": f"文件过大（{len(raw) / 1048576:.1f}MB），"
                                       f"上限 {MAX_UPLOAD_BYTES // 1048576}MB"}, 413)
                return
            try:
                fp = save_upload(name, raw)
            except OSError as exc:
                self._json({"ok": False, "message": f"写入附件库失败：{exc}"}, 500)
                return
            self._json({"ok": True, "name": fp.name, "path": str(fp.resolve()),
                        "size": len(raw), "message": "已上传到本机附件库"})
            return

        # 从附件库删除（只允许删库内文件，避免被当成任意文件删除接口）
        if path == "/api/upload/delete":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            try:
                target = Path(str(body.get("path") or "")).resolve()
                target.relative_to(uploads_dir().resolve())
            except (ValueError, OSError):
                self._json({"ok": False, "message": "只允许删除附件库内的文件"}, 400)
                return
            try:
                if not target.is_file():
                    self._json({"ok": False, "message": "文件不存在或已被删除"}, 404)
                    return
                target.unlink()
            except OSError as exc:
                self._json({"ok": False, "message": f"删除失败：{exc}"}, 500)
                return
            try:
                # 每个附件一个独立时间戳目录：文件删掉后空壳目录一并清理
                # （非空时 rmdir 抛 OSError，静默忽略即可）
                target.parent.rmdir()
            except OSError:
                pass
            self._json({"ok": True, "message": "已从附件库删除"})
            return

        # 在 Finder 中定位会话日志（浏览器无法直接打开 file://，只能由后端代劳）
        if urlparse(self.path).path == "/api/reveal":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            target = str(body.get("path") or "").strip()
            if not target:
                self._json({"ok": False, "message": "缺少 path 参数"}, 400)
                return
            ok, msg = reveal_in_finder(target)
            self._json({"ok": ok, "path": msg if ok else "", "message": msg},
                       200 if ok else 500)
            return
        self._send(404, b"not found", "text/plain")

    def do_DELETE(self):
        """删除轮次归档：DELETE /api/rounds/<run_id>"""
        if not self._same_origin_ok():
            self._json({
                "ok": False, "category": "forbidden",
                "message": ("来源校验未通过：仅允许经 127.0.0.1 / localhost 访问本服务"
                            f"（Host={self.headers.get('Host') or '-'}，"
                            f"Origin={self.headers.get('Origin') or '-'}）"),
            }, 403)
            return
        path = urlparse(self.path).path
        if path.startswith("/api/rounds/"):
            rid = path[len("/api/rounds/"):].strip("/")
            ok, msg = delete_round(rid)
            self._json({"ok": ok, "run_id": rid if ok else "", "message": msg},
                       200 if ok else (404 if "不存在" in msg else 400))
            return
        self._send(404, b"not found", "text/plain")
def main() -> int:
    global ALLOW_LAN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--allow-lan", action="store_true",
                    help="临时放行局域网访问：绑定 0.0.0.0 并放行私网 IP 的 Host/Origin"
                         "（默认仅允许 127.0.0.1 / localhost）")
    args = ap.parse_args()

    if args.allow_lan:
        ALLOW_LAN = True
        if args.host == "127.0.0.1":
            args.host = "0.0.0.0"
        print("⚠ 已临时放行局域网访问（--allow-lan）：私网 IP 可访问本服务，"
              "公网来源仍被拒绝。用完请去掉该参数重启。", flush=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"测试平台服务已启动: http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
