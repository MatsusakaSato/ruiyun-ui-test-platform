"""Excel / CSV 用例表读取（纯标准库，零依赖）。

为什么要自己解析 .xlsx：平台的运行环境只装了 pyyaml + websocket 等极少的包，
openpyxl / pandas 都没有。而 .xlsx 本身就是一个 zip（内含 XML），
用标准库的 zipfile + ElementTree 足够读出单元格文本 —— 不值得为「导入用例」
再拉一个几十 MB 的依赖进来。

支持：
  * .xlsx：共享字符串（含富文本 runs）、内联字符串、数字、布尔、公式缓存值；
  * .xlsm / .xltx：同 xlsx（只多/换后缀）；
  * 单元格日期：按样式里的 numFmt 判定，转成 YYYY-MM-DD；
  * .csv：自动试 UTF-8(BOM) / GBK，逗号或制表符分隔。
""" 
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

# 单元格内可能出现换行；xlsx 用垂直制表符/Tab 表示换行
_CELL_NL = re.compile(r"[\u000b\u000c]")


def _local(tag: str) -> str:
    """{namespace}name -> name（不同 Office 版本的命名空间不一致）。"""
    return tag.rsplit("}", 1)[-1]


def _read_shared_strings(zf: zipfile.ZipFile) -> list:
    """共享字符串表：<si> 下可能是单个 <t>，也可能是多个富文本 <r><t>。"""
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    out = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        parts = [node.text or "" for node in si.iter() if _local(node.tag) == "t"]
        out.append("".join(parts))
    return out


def _date_style_indexes(zf: zipfile.ZipFile) -> set:
    """哪些样式（s 属性）代表日期 —— 只有这些数字才该显示成日期。

    判定方式：numFmtId 落在内置日期区间（14–22、45–47），或自定义格式
    （cellXfs 里 numFmtId>=164）的格式串里含 y/m/d/h 且不含纯文本标记。
    """
    try:
        styles = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom: dict = {}
    for node in styles.iter():
        if _local(node.tag) == "numFmt":
            fid = node.get("numFmtId")
            code = node.get("formatCode") or ""
            if fid:
                custom[int(fid)] = code
    builtin_date = set(range(14, 23)) | set(range(45, 48))
    out = set()
    idx = 0
    for node in styles.iter():
        if _local(node.tag) != "cellXfs":
            continue
        for xf in node:
            if _local(xf.tag) != "xf":
                continue
            try:
                fid = int(xf.get("numFmtId") or 0)
            except ValueError:
                fid = 0
            code = custom.get(fid, "")
            is_date = fid in builtin_date or (
                fid >= 164 and bool(re.search(r"[ymdhs]", code, re.I))
                and not re.search(r'"[^"]*"', code))
            if is_date:
                out.add(idx)
            idx += 1
    return out


def _serial_to_date(value: str) -> str:
    """Excel 日期序列号 → YYYY-MM-DD（1900 历，含 Excel 的闰年 bug 偏移）。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value
    base = datetime(1899, 12, 30)
    d = base + timedelta(days=n)
    if d.hour or d.minute or d.second:
        return d.strftime("%Y-%m-%d %H:%M")
    return d.strftime("%Y-%m-%d")


def _cell_text(cell, shared: list, date_styles: set) -> str:
    """取一个 <c> 的显示文本。"""
    ctype = cell.get("t") or "n"
    try:
        style = int(cell.get("s") or -1)
    except ValueError:
        style = -1
    value_node = None
    inline = None
    for child in cell:
        name = _local(child.tag)
        if name == "v":
            value_node = child
        elif name == "is":                     # 内联字符串
            inline = "".join(n.text or "" for n in child.iter() if _local(n.tag) == "t")
    if ctype == "inlineStr":
        return _CELL_NL.sub("\n", inline or "")
    if value_node is None:
        return ""
    text = value_node.text or ""
    if ctype == "s":                           # 共享字符串下标
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "是" if text == "1" else "否"
    if ctype in ("str", "e"):
        return text
    # 数字：按样式决定要不要当日期显示
    if style in date_styles and text:
        return _serial_to_date(text)
    return text


def _sheet_paths(zf: zipfile.ZipFile) -> list:
    """按 workbook 里的顺序取工作表 XML 路径（不猜 sheet1.xml 的名字）。

    workbook.xml / rels 解析不出来时返回空列表，由调用方按文件名兜底 ——
    有些工具导出的 xlsx 命名空间声明不规范，不该因此整个导入失败。
    """
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return []
    target: dict = {}
    for rel in rels:
        target[rel.get("Id")] = rel.get("Target") or ""
    paths = []
    for node in wb.iter():
        if _local(node.tag) != "sheet":
            continue
        rid = ""
        for k, v in node.attrib.items():
            if _local(k) == "id":
                rid = v
        t = target.get(rid, "")
        if not t:
            continue
        if t.startswith("/"):
            paths.append(t.lstrip("/"))
        elif t.startswith("xl/"):
            paths.append(t)
        else:
            paths.append("xl/" + t.lstrip("./"))
    return paths


def read_xlsx(data: bytes, max_rows: int = 5000) -> dict:
    """读 xlsx 的第一个工作表 → {"rows": [[str]], "sheet": 名字}。"""
    zf = zipfile.ZipFile(io.BytesIO(data))
    shared = _read_shared_strings(zf)
    date_styles = _date_style_indexes(zf)
    paths = _sheet_paths(zf)
    # 只读第一个工作表：用例表通常就一张，按名字猜反而更容易读错
    candidates = paths or [n for n in zf.namelist()
                           if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    if not candidates:
        raise ValueError("这个文件里没有工作表（不是有效的 xlsx？）")
    sheet = candidates[0]
    try:
        root = ET.fromstring(zf.read(sheet))
    except ET.ParseError as exc:
        raise ValueError(f"工作表 XML 无法解析：{exc}")
    rows: list = []
    for row_node in root.iter():
        if _local(row_node.tag) != "row":
            continue
        cells: dict = {}
        for c in row_node:
            if _local(c.tag) != "c":
                continue
            ref = c.get("r") or ""
            m = re.match(r"^([A-Z]+)", ref)
            col = 0
            if m:
                for ch in m.group(1):
                    col = col * 26 + (ord(ch) - 64)          # A=1, AA=27
                col -= 1
            cells[col] = _cell_text(c, shared, date_styles)
        if not cells:
            rows.append([])
            continue
        width = max(cells) + 1
        rows.append([_CELL_NL.sub("\n", cells.get(i, "")) for i in range(width)])
        if len(rows) >= max_rows:
            break
    return {"rows": rows, "sheet": sheet.rsplit("/", 1)[-1].replace(".xml", "")}


def read_csv(data: bytes, max_rows: int = 5000) -> dict:
    """读 csv / tsv：先按 UTF-8(BOM) 解，失败再按 GBK（Excel 另存常见编码）。"""
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别文件编码（试过 UTF-8 / GBK）")
    head = text[:4096]
    delim = "\t" if head.count("\t") > head.count(",") else ","
    rows = []
    for r in csv.reader(io.StringIO(text), delimiter=delim):
        rows.append([(c or "").strip() for c in r])
        if len(rows) >= max_rows:
            break
    return {"rows": rows, "sheet": "csv"}


def read_table(filename: str, data: bytes, max_rows: int = 5000) -> dict:
    """按后缀分发；xlsx 系列走 zip 解析，其余按文本表处理。"""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        try:
            return read_xlsx(data, max_rows=max_rows)
        except zipfile.BadZipFile:
            raise ValueError("这个文件不是有效的 xlsx（.xls 老格式请先另存为 .xlsx）")
    return read_csv(data, max_rows=max_rows)


# ------------------------------------------------------------------ 列识别
# 提问列的候选表头（避免命中「输入类型」等非提问列）
PROMPT_HEADERS = ("query内容", "query", "提问内容", "问题内容", "提问", "提示词", "问题",
                  "用例", "任务", "prompt", "question", "case", "command")
SCENE_HEADERS = ("场景", "分类", "模块", "业务", "scene", "category")
TARGET_HEADERS = ("测试目标", "目标", "产物", "target", "expect")
NAME_HEADERS = ("序号", "编号", "用例名称", "用例名", "名称", "标题", "id", "name", "seq", "no")
ATTACHMENT_HEADERS = ("附件", "参考文件", "文件", "附件名", "attachment", "attachments", "file")
SKILL_HEADERS = ("skill", "技能", "期望工具", "工具", "expect_tools", "tools")

# 允许出现在「测试目标」里的值（与预设用例库的 labels 口径一致）
KNOWN_TARGETS = ("word", "ppt", "html", "pdf", "excel", "network")


def _is_header_like(value: str) -> bool:
    """表头判定用：短、不含句末标点、不是明显的一句话。"""
    v = (value or "").strip()
    return bool(v) and len(v) <= 12 and not re.search(r"[。？?!！,，;；]", v)


def infer_targets(prompt: str, attachment_name: str = "", raw_target: str = "") -> list[str]:
    """从显式目标列、附件后缀或提问语义推断测试目标（word, ppt, excel, pdf, html, network）。"""
    targets = []
    # 1. 显式白名单匹配
    if raw_target:
        targets = [t for t in KNOWN_TARGETS
                   if re.search(rf"(?<![a-z]){t}(?![a-z])", raw_target, re.I)]
        if targets:
            return targets

    # 2. 从附件文件名后缀推断
    att_lower = (attachment_name or "").lower()
    if any(att_lower.endswith(ext) for ext in (".docx", ".doc", ".dotx")):
        targets.append("word")
    elif any(att_lower.endswith(ext) for ext in (".pptx", ".ppt", ".potx")):
        targets.append("ppt")
    elif any(att_lower.endswith(ext) for ext in (".xlsx", ".xls", ".csv")):
        targets.append("excel")
    elif att_lower.endswith(".pdf"):
        targets.append("pdf")
    elif any(att_lower.endswith(ext) for ext in (".html", ".htm")):
        targets.append("html")

    # 3. 从提问文本语义推断
    try:
        from core.case_intent import infer_target_kinds
        kinds, _ = infer_target_kinds(prompt)
        kind_map = {
            "docx": "word", "pptx": "ppt", "excel": "excel",
            "pdf": "pdf", "html": "html", "network": "network"
        }
        for k in kinds:
            mapped = kind_map.get(k)
            if mapped and mapped not in targets:
                targets.append(mapped)
    except Exception:
        pass

    # 4. 中文关键词特征补全
    p_lower = (prompt or "").lower()
    if not targets:
        if any(w in p_lower for w in ["教案", "教学设计", "文档", "word", "撰写", "通报", "总结", "方案", "通知", "倡议书", "计划", "发言稿", "讲话稿"]):
            targets.append("word")
        elif any(w in p_lower for w in ["ppt", "课件", "幻灯片", "演示文稿"]):
            targets.append("ppt")
        elif any(w in p_lower for w in ["excel", "表格", "成绩表", "统计表", "排班表", "清单"]):
            targets.append("excel")
        elif "pdf" in p_lower:
            targets.append("pdf")
        elif "html" in p_lower or "网页" in p_lower:
            targets.append("html")

    # 5. 兜底为 word
    if not targets:
        targets.append("word")

    return targets


def detect_columns(rows: list) -> dict:
    """认出表头行与各列下标（不取值）。

    返回 {header, head_idx, prompt_col, scene_col, target_col, name_col, attachment_col, skill_col, total_rows, preview}
    识别不出的列一律留 None —— 不硬套，界面会让用户自己选列。
    """
    rows = [list(r or []) for r in (rows or [])]
    while rows and not any((c or "").strip() for c in rows[-1]):
        rows.pop()                       # 去掉尾部整行空行
    if not rows:
        return {"header": [], "head_idx": None, "prompt_col": 0, "scene_col": None,
                "target_col": None, "name_col": None, "attachment_col": None,
                "skill_col": None, "total_rows": 0, "preview": []}

    def width(r):
        return max((i for i, c in enumerate(r) if (c or "").strip()), default=-1) + 1

    # 表头行：前 10 行里「非空单元格最多、且每格都像表头」的那一行
    head_idx, best = None, 0
    for i, r in enumerate(rows[:10]):
        cells = [c for c in r if (c or "").strip()]
        if not cells:
            continue
        if len(cells) > best and all(_is_header_like(c) for c in cells):
            head_idx, best = i, len(cells)
    header = rows[head_idx] if head_idx is not None else []
    body = rows[(head_idx + 1) if head_idx is not None else 0:]
    width = max([width(header)] + [width(r) for r in body] + [1])

    def header_col(names, exclude=(), forbid=()):
        for i in range(width):
            text = ((header[i] if i < len(header) else "") or "").strip().lower()
            if not text or i in exclude:
                continue
            if any(f.lower() in text for f in forbid):
                continue
            for nm in names:
                if nm.lower() in text:
                    return i
        return None

    # 提问列排除含有「类型/type」的列（如「输入类型」不是提问）
    prompt_col = header_col(PROMPT_HEADERS, forbid=("类型", "type"))
    scene_col = header_col(SCENE_HEADERS,
                           exclude=({prompt_col} if prompt_col is not None else set()))
    name_col = header_col(NAME_HEADERS,
                          exclude={c for c in (prompt_col, scene_col) if c is not None})
    attachment_col = header_col(ATTACHMENT_HEADERS,
                                exclude={c for c in (prompt_col, scene_col, name_col) if c is not None})
    skill_col = header_col(SKILL_HEADERS,
                           exclude={c for c in (prompt_col, scene_col, name_col, attachment_col) if c is not None})
    target_col = header_col(TARGET_HEADERS,
                            exclude={c for c in (prompt_col, scene_col, name_col, attachment_col, skill_col) if c is not None},
                            forbid=("输入类型",))

    if prompt_col is None:
        # 没有可识别的表头 → 取「正文里平均文本最长」的一列当提问列
        sums, counts = {}, {}
        for r in body:
            for i in range(width):
                v = (r[i] if i < len(r) else "") or ""
                if v.strip():
                    sums[i] = sums.get(i, 0) + len(v)
                    counts[i] = counts.get(i, 0) + 1
        prompt_col = max(sums, key=lambda i: sums[i] / max(1, counts[i]), default=0)

    return {
        "header": [c or "" for c in header],
        "head_idx": head_idx,
        "prompt_col": prompt_col,
        "scene_col": scene_col,
        "target_col": target_col,
        "name_col": name_col,
        "attachment_col": attachment_col,
        "skill_col": skill_col,
        "total_rows": len(body),
        "preview": body,
    }


def rows_to_items(rows: list, head_idx, prompt_col: int,
                  scene_col=None, target_col=None,
                  name_col=None, attachment_col=None,
                  skill_col=None) -> dict:
    """按给定列下标从数据行里取值。

    返回 {items, skipped}：items 每项包含 prompt, scene, targets, name, attachments 等；
    提问列为空的行会被跳过并计入 skipped。
    """
    body = rows[(head_idx + 1) if isinstance(head_idx, int) else 0:]
    items, skipped = [], 0
    for r in body:
        get = lambda i: ((r[i] if isinstance(i, int) and 0 <= i < len(r) else "") or "").strip()
        prompt = get(prompt_col)
        if not prompt:
            if any((c or "").strip() for c in r):
                skipped += 1
            continue

        scene = get(scene_col)
        raw_targets = get(target_col)
        att_val = get(attachment_col)
        name_val = get(name_col)
        skill_val = get(skill_col)

        # 智能推断或白名单匹配测试目标
        targets = infer_targets(prompt, attachment_name=att_val, raw_target=raw_targets)

        # 附件与输入形式
        attachments = [att_val] if att_val else []
        has_attachment = bool(attachments or any("附件" in str(c) for c in r))

        # 名称与 ID 生成
        cid = None
        if name_val.isdigit():
            seq_num = int(name_val)
            cid = f"CASE-{seq_num:03d}"
            cname = f"{scene}-{seq_num:03d}" if scene else f"用例-{seq_num:03d}"
        elif name_val:
            cname = name_val
        else:
            cname = ""

        # 期望工具
        expect_tools = [s.strip() for s in skill_val.split(",") if s.strip()] if skill_val else []

        # 构造标签与元数据
        labels = {
            "scene": scene,
            "targets": targets,
            "attachment": has_attachment,
        }
        header = rows[head_idx] if isinstance(head_idx, int) and 0 <= head_idx < len(rows) else []
        known_cols = {c for c in (prompt_col, scene_col, target_col, name_col, attachment_col, skill_col) if c is not None}
        for j, h in enumerate(header):
            if j not in known_cols:
                h_name = (h or "").strip()
                val = get(j)
                if h_name and val:
                    if "学科" in h_name:
                        labels["subject"] = val
                    elif "难度" in h_name:
                        labels["difficulty"] = val
                    elif "输入类型" in h_name:
                        labels["input_type"] = val
                    elif "强项" in h_name:
                        labels["is_strength"] = val
                    else:
                        labels[h_name] = val

        item = {
            "prompt": _CELL_NL.sub("\n", prompt),
            "scene": scene,
            "targets": targets,
            "raw_target": raw_targets,
            "name": cname,
            "attachments": attachments,
            "attachment": has_attachment,
            "expect_tools": expect_tools,
            "labels": labels,
        }
        if cid:
            item["id"] = cid

        items.append(item)

    return {"items": items, "skipped": skipped}


def detect_table(rows: list) -> dict:
    """一步到位：认列 + 取值（供不便分两步的调用方使用）。"""
    meta = detect_columns(rows)
    got = rows_to_items(rows, meta["head_idx"], meta["prompt_col"],
                        meta["scene_col"], meta["target_col"],
                        meta.get("name_col"), meta.get("attachment_col"),
                        meta.get("skill_col"))
    return {k: v for k, v in meta.items() if k != "preview"} | got

