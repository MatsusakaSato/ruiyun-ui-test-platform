"""应用与日志路径的用户自定义设置。

让用户自行配置两个本机相关项，不配置则使用内置默认地址：
  * app.binary        —— 被测应用可执行文件路径
  * paths.session_root —— 被测应用产生的会话日志根目录（sess_*/ 的父目录）

三层合并优先级：`.app_settings.json`（界面「应用设置」写入）> config.yaml > 内置默认。
config.yaml 保持唯一"静态配置入口"不变；界面覆盖项放独立文件（0600、不进版本库），
原因与 .llm_secrets.json 相同 —— 用文本方式改 config.yaml 会破坏注释与排版。

仅使用标准库，供 run_pipeline / run_repro / server / discover_ui 复用。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 内置默认地址
# 不配置任何一项时的回退值（expanduser 支持 ~ 写法）
import sys
import os

if sys.platform == "win32":
    # Windows 安装位置：C:\Program Files\srtclaw\睿云智能工作台.exe
    # 用 %ProgramFiles% 而非写死 "C:\Program Files"：系统盘/语言不同的机器上
    # 该变量才是权威值；取不到时才回退到英文默认路径。
    DEFAULT_APP_BINARY = os.path.join(
        os.environ.get("ProgramFiles") or r"C:\Program Files",
        "srtclaw", "睿云智能工作台.exe")
else:
    DEFAULT_APP_BINARY = "/Applications/睿云智能工作台.app/Contents/MacOS/睿云智能工作台"
DEFAULT_SESSION_ROOT = "~/.srtclaw/workspace/session"

# 项目内置配置模版：全新环境（工作区里还没有 config.yaml）时按它初始化，
# 否则「环境」下拉、断言阈值等依赖配置的功能会整片失效（见 config_path）。
TEMPLATE_CONFIG = _ROOT / "config.template.yaml"

# 界面覆盖文件：与 .llm_secrets.json 同模式（0600、gitignore）
SETTINGS_FILE = _ROOT / ".app_settings.json"

# 来源标识（界面据此显示"这个值从哪来"）
SRC_LOCAL = "local"      # .app_settings.json 显式覆盖
SRC_CONFIG = "config"    # config.yaml 声明（非空）
SRC_DEFAULT = "default"  # 内置默认（config.yaml 留空/缺省且无覆盖）


def load_overrides() -> dict:
    """读取界面覆盖项；文件缺失/损坏一律返回空覆盖，不向调用方抛异常。"""
    if not SETTINGS_FILE.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "app_binary": str(data.get("app_binary") or "").strip(),
        "session_root": str(data.get("session_root") or "").strip(),
        "user_workspace": str(data.get("user_workspace") or "").strip(),
    }


def save_overrides(app_binary: str, session_root: str,
                   user_workspace: str = "") -> dict:
    """原子写入覆盖文件并设为 0600。空值表示清除该项覆盖（回退 config.yaml/默认）。"""
    payload = {
        "app_binary": (app_binary or "").strip(),
        "session_root": (session_root or "").strip(),
        "user_workspace": (user_workspace or "").strip(),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_FILE.parent),
                               prefix=".app_settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass
    return load_overrides()


def clear_overrides() -> None:
    """删除覆盖文件（全部恢复 config.yaml / 内置默认）。"""
    try:
        SETTINGS_FILE.unlink()
    except OSError:
        pass


def _clean(v) -> str:
    s = str(v or "").strip()
    return s if s and s not in ("null", "~") else ""


def effective_config(cfg: dict) -> dict:
    """按三层优先级补全 app.binary 与 paths.session_root / workspace_root。

    * app.binary：覆盖 > config > 内置默认
    * paths.session_root：覆盖 > config > 内置默认
    * paths.workspace_root：覆盖(config) > session_root 的父目录（派生默认）
    全部做 expanduser（支持 ~ 与相对工作区的写法）。返回深拷贝，不改入参。
    """
    cfg = copy.deepcopy(cfg or {})
    ov = load_overrides()
    app = cfg.setdefault("app", {})
    paths = cfg.setdefault("paths", {})

    binary = _clean(ov.get("app_binary")) or _clean(app.get("binary")) or DEFAULT_APP_BINARY
    app["binary"] = str(Path(binary).expanduser())

    session_root = (_clean(ov.get("session_root")) or _clean(paths.get("session_root"))
                    or DEFAULT_SESSION_ROOT)
    sr = Path(session_root).expanduser()
    paths["session_root"] = str(sr)

    wr = _clean(paths.get("workspace_root"))
    paths["workspace_root"] = str(Path(wr).expanduser()) if wr else str(sr.parent)
    return cfg


# ---------------------------------------------------------------- 用户工作区
# 非功能性数据（预设用例 / 附件库 / 轮次归档 / config）集中存放在用户主目录下，
# 与代码分离，用户可在文件管理器直接查看与编辑。跨平台取 home：
#   macOS ~/、Windows %USERPROFILE%，目录名 .ruiyun-autotest
# 可通过覆盖文件的 user_workspace 键指到任意目录。
DEFAULT_WORKSPACE = str(Path.home() / ".ruiyun-autotest")

# 历史遗留位置（按优先级排列）：应用迭代中数据曾存放的位置。
# 目标不存在时按顺序找第一个存在的旧位置搬过来，实现无缝升级。
_LEGACY_TESTCASES = [_ROOT / "testcases.yaml", _ROOT / "workspace" / "testcases.yaml"]
_LEGACY_UPLOADS = [_ROOT / "artifacts" / "uploads", _ROOT / "workspace" / "uploads"]
_LEGACY_ROUNDS = [_ROOT / "artifacts" / "rounds", _ROOT / "workspace" / "rounds"]
_LEGACY_CONFIG = [_ROOT / "config.yaml"]
_LEGACY_SECRETS = [_ROOT / ".llm_secrets.json"]


def user_workspace() -> Path:
    """用户工作区目录（自动创建）。"""
    ov = load_overrides()
    ws = _clean(ov.get("user_workspace")) or DEFAULT_WORKSPACE
    p = Path(ws).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _migrate_legacy(src: Path, dst: Path) -> None:
    """把旧位置的数据一次性搬到用户工作区（move 保留内容，幂等）。

    只在「新位置不存在、旧位置存在」时迁移 —— 已迁移过或新位置已有数据
    （用户自己在工作区建了新文件）都不动。失败不抛异常：读不到就当空数据。
    """
    if dst.exists() or not src.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError:
        pass


def _migrate_first(sources: list, dst: Path) -> None:
    """按优先级迁移：取第一个存在的旧位置搬到 dst。"""
    for src in sources:
        if dst.exists():
            return
        _migrate_legacy(src, dst)


def preset_path() -> Path:
    """预设用例文件：工作区/testcases.yaml（历史位置自动迁移）。"""
    p = user_workspace() / "testcases.yaml"
    _migrate_first(_LEGACY_TESTCASES, p)
    # 用户把工作区改到别处时，默认工作区里的既有数据也要跟过去 ——
    # 否则切目录等于「数据消失」，找不回的体验不可接受
    if str(user_workspace()) != DEFAULT_WORKSPACE:
        _migrate_legacy(Path(DEFAULT_WORKSPACE) / "testcases.yaml", p)
    return p


def uploads_dir() -> Path:
    """附件库目录：工作区/uploads（历史位置自动迁移）。"""
    d = user_workspace() / "uploads"
    _migrate_first(_LEGACY_UPLOADS, d)
    if str(user_workspace()) != DEFAULT_WORKSPACE:
        _migrate_legacy(Path(DEFAULT_WORKSPACE) / "uploads", d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def rounds_dir() -> Path:
    """轮次归档目录：工作区/rounds（历史位置自动迁移）。"""
    d = user_workspace() / "rounds"
    _migrate_first(_LEGACY_ROUNDS, d)
    if str(user_workspace()) != DEFAULT_WORKSPACE:
        _migrate_legacy(Path(DEFAULT_WORKSPACE) / "rounds", d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    """平台配置文件：工作区/config.yaml（历史位置自动迁移，缺失时按模版自愈初始化）。

    全新克隆的机器上工作区里没有任何文件，config.yaml 自然也不存在。
    此时若返回一个不存在的路径，读配置的调用方会直接抛 FileNotFoundError：
    界面「环境」下拉读到 500 后变成空选择框（没字、点不动），流水线也拿不到
    断言阈值。因此这里在「工作区没有、仓库根也没有」时就地按内置模版初始化一份。
    """
    p = user_workspace() / "config.yaml"
    _migrate_first(_LEGACY_CONFIG, p)
    if str(user_workspace()) != DEFAULT_WORKSPACE:
        _migrate_legacy(Path(DEFAULT_WORKSPACE) / "config.yaml", p)
    if not p.is_file() and TEMPLATE_CONFIG.is_file():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(TEMPLATE_CONFIG), str(p))
        except OSError:
            pass
    return p


def secrets_path() -> Path:
    """模型密钥文件：工作区/.llm_secrets.json（历史位置自动迁移）。"""
    p = user_workspace() / ".llm_secrets.json"
    _migrate_first(_LEGACY_SECRETS, p)
    if str(user_workspace()) != DEFAULT_WORKSPACE:
        _migrate_legacy(Path(DEFAULT_WORKSPACE) / ".llm_secrets.json", p)
    return p


def _source(ov_val: str, cfg_val: str) -> str:
    if _clean(ov_val):
        return SRC_LOCAL
    if _clean(cfg_val):
        return SRC_CONFIG
    return SRC_DEFAULT


def describe(cfg: dict) -> dict:
    """界面「应用设置」状态：当前生效值、来源、路径是否存在。

    来源判断必须用**合并前**的原始 config 值 —— 若在 effective_config
    之后取，默认值已被填入，所有项都会被误标为 config。

    另外下发 platform / is_windows / defaults：界面输入框的 placeholder 与
    「留空使用默认」提示必须显示**本机**的默认路径。前端曾把 macOS 的
    .app/Contents/MacOS/... 写死在 HTML 里，Windows 上即便服务端已经算出
    C:\Program Files\srtclaw\...，用户看到的仍是 mac 路径。
    """
    cfg = cfg or {}
    ov = load_overrides()
    raw_app = cfg.get("app") or {}
    raw_paths = cfg.get("paths") or {}
    eff = effective_config(cfg)
    app = eff.get("app") or {}
    paths = eff.get("paths") or {}
    ws = user_workspace()

    binary = str(app.get("binary") or "")
    sr = str(paths.get("session_root") or "")
    return {
        "platform": sys.platform,
        "is_windows": sys.platform == "win32",
        # 内置默认（与用户是否覆盖无关）：界面据此提示「留空时用什么」
        "defaults": {
            "binary": str(Path(DEFAULT_APP_BINARY).expanduser()),
            "session_root": str(Path(DEFAULT_SESSION_ROOT).expanduser()),
        },
        "binary": {
            "value": binary,
            "source": _source(ov.get("app_binary"), raw_app.get("binary")),
            "exists": Path(binary).exists() if binary else False,
        },
        "session_root": {
            "value": sr,
            "source": _source(ov.get("session_root"), raw_paths.get("session_root")),
            "exists": Path(sr).is_dir() if sr else False,
        },
        "workspace_root": {"value": str(paths.get("workspace_root") or "")},
        # 用户工作区（非功能性数据：预设用例 / 附件库）；config.yaml 无此配置项，
        # 来源只有 本机覆盖 / 内置默认 两种
        "workspace": {
            "value": str(ws),
            "source": SRC_LOCAL if _clean(ov.get("user_workspace")) else SRC_DEFAULT,
            "exists": ws.is_dir(),
        },
    }
