"""从工具调用中抽取「产出物」及其正文。

实测结论（2026-09 会话日志采样，证据见交付说明）：
文件产出型工具的**正文落在 arguments**，result 只回传路径与元数据：
  * mcp_write_workspace_file : arguments.relative_path / arguments.content
  * convert_markdown_to_docx : arguments.md_file_name / arguments.markdown_content
                               （markdown_content 常为空，真实正文是同名 .md 的前序写入）
  * generate_image           : arguments.filename / arguments.prompt
因此抽取必须同时看 arguments 与 result，并对「先写 .md 再转 docx」做**同名回填**。
**不解析 docx/pptx/xlsx 等二进制**。

本模块被 core/evaluator.py、core/format_check.py、core/safety_scan.py 共用。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 抽取逻辑版本：产物清单会随抽取规则修正而变（例如「失败的调用不算产物」
# 「脚本里写出的文件要算产物」）。归档里记下版本号，读取方发现版本落后就**重算**，
# 否则老轮次会永远带着当时抽错的清单（实测踩到过：指向不存在文件的幽灵产物）。
EXTRACT_VERSION = 2


# 扩展名 → 归一化产物类型
EXT_KINDS = {
    "docx": "docx", "doc": "docx",
    "pptx": "pptx", "ppt": "pptx",
    "xlsx": "excel", "xls": "excel", "csv": "excel",
    "pdf": "pdf",
    "html": "html", "htm": "html",
    "md": "md", "markdown": "md",
    "txt": "txt",
    "png": "image", "jpg": "image", "jpeg": "image",
    "webp": "image", "gif": "image", "svg": "image",
}

# 用例 labels.targets 词表 → 产物类型集合
TARGET_KINDS = {
    "word": {"docx"},
    "ppt": {"pptx"},
    "html": {"html"},
    "excel": {"excel"},
    "pdf": {"pdf"},
    "network": set(),   # 联网检索不是产物类型
}

# 产出型工具识别：名字命中关键词，且不在排除表中
_PRODUCER_RE = re.compile(r"(write|save|export|generate|convert|render|download|upload)", re.I)
_EXCLUDE_TOOLS = {
    "todo_create", "todo_complete", "todo_remove",
    "cron_create_job", "cron_preview_job",
    "read_memory", "write_memory", "edit_memory",
    "read_file", "read_skill_file", "grep_files", "search_session",
}

# result 里的路径字段描述的是「产出物」；arguments 里的可能是「源文件」——
# 例如 convert_markdown_to_docx: args.md_file_name 是源 .md，
# result.md_file_name 才是产出的 .docx。故必须优先读 result。
_RESULT_PATH_KEYS = ("md_file_name", "save_image_path", "relative_path",
                     "output_path", "file_path", "full_path", "path")
_ARG_PATH_KEYS = ("relative_path", "output_path", "file_path",
                  "filename", "file_name", "path")
_SOURCE_NAME_KEYS = ("md_file_name",)   # 转换类工具：args 里给的是源文件名
_FULL_KEYS = ("full_path", "workspace_path")
_TEXT_KEYS = ("content", "markdown_content", "file_content", "markdown", "text", "body")


@dataclass
class Artifact:
    """一件产出物。text 为空表示只能确认存在、无法取得正文（如二进制产物）。"""

    kind: str
    rel_path: str = ""
    full_path: str = ""
    text: str = ""
    source_tool: str = ""
    note: str = ""


@dataclass
class ArtifactSet:
    items: list = field(default_factory=list)

    @property
    def kinds(self) -> set:
        return {a.kind for a in self.items}

    @property
    def paths(self) -> list:
        return [a.rel_path or a.full_path for a in self.items if (a.rel_path or a.full_path)]

    def texts(self) -> list:
        """(标签, 正文) —— 供安全扫描与判分输入使用。"""
        out = []
        for a in self.items:
            if a.text:
                out.append((f"产出物:{a.rel_path or a.full_path or a.kind}", a.text))
        return out


def kind_of(path: str) -> str:
    # 归一化反斜杠：Windows 路径（C:\a\b.png）若直接进入此函数也能取对文件名
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return EXT_KINDS.get(name.rsplit(".", 1)[-1].lower(), "")


def _stem(path: str) -> str:
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0].lower() if "." in name else name.lower()


def _pick(d: dict, keys) -> str:
    for k in keys:
        v = (d or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def is_producer(tool_name: str) -> bool:
    n = tool_name or ""
    return bool(_PRODUCER_RE.search(n)) and n not in _EXCLUDE_TOOLS


def _call_failed(obj: dict) -> bool:
    """该次工具调用是否**明确失败**。

    实测事故：`convert_markdown_to_docx` 返回
    `{"success": false, "error": "File not found: 空白文档.docx.md"}` 时，
    旧实现照样把它记成一件产物 —— 界面上就出现一个指向**不存在文件**的链接，
    而真正产出的 `空白文档.docx`（由后续脚本生成）反倒没被记录。
    判定刻意保守：只有明确 `success: false`，或有 error 且没有 `success: true`
    才算失败；结果里没有这些字段的工具一律按成功处理（不误杀）。
    """
    if not isinstance(obj, dict) or not obj:
        return False
    ok = obj.get("success")
    if ok is False:
        return True
    return bool(obj.get("error")) and ok is not True


def extract_artifacts(tool_calls) -> ArtifactSet:
    """从工具调用序列抽取产出物。tool_calls 为 core.models.ToolCall 列表。"""
    written: dict = {}   # 已写入文件的正文：stem(小写) -> text，供转换类回填
    items: list = []

    for tc in tool_calls or []:
        name = getattr(tc, "name", "") or ""
        args = getattr(tc, "arguments", None)
        args = args if isinstance(args, dict) else {}
        obj = getattr(tc, "result_obj", None)
        obj = obj if isinstance(obj, dict) else {}

        # 失败的调用既不记产物、也不参与「同名正文回填」
        if _call_failed(obj):
            continue

        body = _pick(args, _TEXT_KEYS)
        rel_arg = _pick(args, ("relative_path",))
        if rel_arg and body:
            written[_stem(rel_arg)] = body

        if not is_producer(name):
            continue

        path = (_pick(obj, _RESULT_PATH_KEYS)
                or _pick(args, _ARG_PATH_KEYS)
                or _pick(args, _SOURCE_NAME_KEYS))
        if not path:
            continue

        full = _pick(obj, _FULL_KEYS) or _pick(args, ("full_path",))
        text = body or _pick(obj, _TEXT_KEYS)
        note = ""
        if not text:
            back = written.get(_stem(path))
            if back:
                text, note = back, "正文由同名写入调用回填"
            else:
                note = "仅确认产物存在，正文不可抽取"
        if kind_of(path) == "image":
            note = (note + "；图像为二进制产物，正文不可抽取").lstrip("；")

        items.append(Artifact(
            kind=kind_of(path) or "other",
            rel_path=path, full_path=full, text=text,
            source_tool=name, note=note,
        ))

    # 补一层：脚本真正写出来的文件
    items.extend(_artifacts_from_commands(tool_calls, items))

    # 同一产物可能被多次引用（写入 + 转换 / 写入 + 脚本）：按 (类型, 路径) 去重，
    # 优先保留有正文的
    uniq: dict = {}
    for a in items:
        k = (a.kind, (a.rel_path or a.full_path).lower())
        cur = uniq.get(k)
        if cur is None or (not cur.text and a.text):
            uniq[k] = a
    return ArtifactSet(items=list(uniq.values()))


# 「执行类」工具：它们的参数里可能明写了脚本要写出的文件路径
_EXEC_TOOL_RE = re.compile(r"(exec|run|shell|command|script)", re.I)
# 绝对路径 + 产物后缀。同时容忍正斜杠写法；不跨引号/换行/分号，避免吞掉整段脚本
_ABS_ARTIFACT_RE = re.compile(
    r"[A-Za-z]:[\\/][^\"'<>|\r\n;]{0,200}?\.(?:docx|doc|pptx|ppt|xlsx|xls|csv|pdf"
    r"|html|htm|md|txt|png|jpe?g|webp|gif|svg)",
    re.I)


def _artifacts_from_commands(tool_calls, existing: list) -> list:
    """从「执行类 / 产出型」调用的参数文本里识别脚本真正写出的产物。

    实测事故：agent 用 `mcp_exec_command` 跑 python 脚本生成了
    `空白文档.docx`（随后把临时脚本删掉），而**唯一的产出型工具
    `convert_markdown_to_docx` 还失败了** —— 结果平台一件产物都指不出来，
    可真实交付物就躺在工作区里。脚本/命令正文通常会明写输出路径，
    这里把它捞出来。

    两条护栏，避免把「只是被读过的文件」算成产物：
      1) 只扫执行类工具与产出型工具的**参数**（不做全工具扫描，read_file 之类不参与）；
      2) 路径必须**真实存在**才记录（存在性校验），并在 note 里写明来源，
         让界面能如实标注「按脚本/命令里的路径识别」，而不是假装是工具申报的产物。
    """
    known = {(a.rel_path or a.full_path).lower() for a in existing}
    out: list = []
    seen: set = set()
    for tc in tool_calls or []:
        name = getattr(tc, "name", "") or ""
        if not (_EXEC_TOOL_RE.search(name) or is_producer(name)):
            continue
        obj = getattr(tc, "result_obj", None)
        if _call_failed(obj if isinstance(obj, dict) else {}):
            continue
        args = getattr(tc, "arguments", None)
        blob = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else ""
        for raw in _ABS_ARTIFACT_RE.findall(blob):
            p = raw.replace("\\\\", "\\").replace("/", "\\").strip()
            low = p.lower()
            if low in seen or low in known:
                continue
            seen.add(low)
            try:
                if not Path(p).is_file():
                    continue
            except (OSError, ValueError, RuntimeError):
                continue
            out.append(Artifact(
                kind=kind_of(p) or "other",
                rel_path=p, full_path=p, text="",
                source_tool=name,
                note="按脚本/命令里出现的输出路径识别（文件确实存在）",
            ))
    return out


def resolve_abs_path(artifact: "Artifact", workspace_root: str = "") -> str:
    """把产物解析成本机绝对路径；解析不到返回空串。

    用途：界面「查看产物」按钮要调 `POST /api/reveal`（后端在文件管理器中定位），
    而它只认绝对路径，事实包里记的却常是 `rel_path`。

    解析顺序（逐级回落，**只读且有界**）：
      1) `full_path`（工具结果里带 `full_path/workspace_path` 时最可靠）
         —— 注意：**应用自己报的 workspace_path 才是最权威的根**。实测本机应用
         写的是 `C:\\Users\\<用户>\\Documents\\srtclaw\\workspace`，与我们在
         config 里假定的 `~/.srtclaw/workspace` **不是同一个目录**；若直接拿
         配置里的根去拼相对路径，产物会被错误地解析到另一个（可能不存在的）位置。
      2) `rel_path` 本身已是绝对路径
      3) `full_path/workspace_path 根 / rel_path`（应用自报根，优先于配置根）
      4) `workspace_root / rel_path`（配置根）
      5) `workspace_root / 去掉盘符式前缀后的 rel_path`
         （实测产物路径偶有 `C:/Users/...` 这类跨平台写法，在本机需剥掉前缀）
      6) 在 `workspace_root` 下按**文件名**有界查找（限深度与访问量，避免全量扫描）

    解析不到时**不隐藏条目**：调用方保留原 rel_path 并在界面标注「本机未找到」，
    与平台「不假成功、如实呈现」的口径一致。
    """
    def _ok(p) -> str:
        try:
            q = Path(p).expanduser()
        except (OSError, ValueError, RuntimeError):
            return ""
        return str(q) if q.is_absolute() and q.is_file() else ""

    rel = (artifact.rel_path or "").strip().replace("\\", "/")
    full = (artifact.full_path or "").strip()
    roots: list = []
    if full:
        try:
            fp = Path(full).expanduser()
        except (OSError, ValueError, RuntimeError):
            fp = None
        if fp is not None:
            # workspace_path 可能是**目录**（应用自报工作区根），也可能已是文件全路径
            if fp.suffix == "" or fp.is_dir():
                roots.append(str(fp))
            else:
                roots.append(str(fp.parent))
    cands = []
    if full:
        cands.append(full)
    if rel:
        cands.append(rel)
        for r in roots + ([(workspace_root or "").strip().rstrip("/")]
                          if (workspace_root or "").strip() else []):
            r = r.rstrip("/\\")
            if r:
                cands.append(f"{r}/{rel.lstrip('/')}")
                cands.append(f"{r}/{re.sub(r'^[A-Za-z]:/', '', rel).lstrip('/')}")
    for c in cands:
        hit = _ok(c)
        if hit:
            return hit

    name = rel.rsplit("/", 1)[-1] if rel else ""
    # 兜底查找的根：应用自报根优先（它就是产物真正落地的地方），再退回配置根
    search_roots = [r for r in (roots + [(workspace_root or "").strip()]) if r]
    for root in search_roots:
        hit = _search_by_name(root, name, rel)
        if hit:
            return hit
    return ""


def _is_file(path) -> bool:
    """路径是否指向一个真实存在的文件（失败一律按「否」处理）。"""
    try:
        return Path(path).expanduser().is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def _search_by_name(root: str, name: str, rel: str) -> str:
    """在 root 下按文件名有界查找；找不到返回空串。"""
    if not name or not root or not Path(root).expanduser().is_dir():
        return ""
    # 按文件名查找时要求「后缀命中」而不是只比文件名：同名文件在工作区里很常见
    # （README.md / 设计稿.pptx…），只比文件名会定位到**另一个**文件并在 Finder 里
    # 选中错的东西。要求至少最后两级路径（目录名 + 文件名）一致，否则宁可不给路径，
    # 由界面显示「本机未找到」。
    rel_parts = [p.lower() for p in rel.split("/") if p and p not in (".", "..")]
    need = 1 if len(rel_parts) <= 1 else 2
    base = Path(root).expanduser()
    best = ""
    best_score = 0
    visited = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        depth = len(Path(dirpath).relative_to(base).parts)
        if depth >= 4:                      # 只找浅层：产物都写在空间根/一级子目录
            dirnames[:] = []
        visited += len(filenames)
        if name in filenames:
            cand = Path(dirpath) / name
            cand_parts = [p.lower() for p in cand.parts]
            score = 0
            for a, b in zip(reversed(rel_parts), reversed(cand_parts)):
                if a != b:
                    break
                score += 1
            if score > best_score and _is_file(cand):
                best, best_score = str(cand), score
                if best_score >= len(rel_parts):    # 整段命中，无需继续找
                    break
        if visited > 4000:                  # 有界：超大工作区不做全量遍历
            break
    return best if best_score >= need else ""


def kinds_from_targets(targets) -> set:
    """把用例 labels.targets 词表翻译成产物类型集合。"""
    out = set()
    for t in (targets or []):
        out |= TARGET_KINDS.get(str(t).strip().lower(), set())
    return out
