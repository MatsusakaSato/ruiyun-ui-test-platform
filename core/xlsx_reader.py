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
# 提问列的候选表头（命中即用；都没有时回落到「最长的文本列」）
PROMPT_HEADERS = ("提问", "提示词", "问题", "用例", "任务", "输入", "prompt",
                  "question", "case", "input", "query", "command")
SCENE_HEADERS = ("场景", "分类", "模块", "业务", "scene", "category")
TARGET_HEADERS = ("测试目标", "目标", "产物", "类型", "输出", "target", "expect", "type")
# 允许出现在「测试目标」里的值（与预设用例库的 labels 口径一致）
KNOWN_TARGETS = ("word", "ppt", "html", "pdf", "excel", "network")


def _is_header_like(value: str) -> bool:
    """表头判定用：短、不含句末标点、不是明显的一句话。"""
    v = (value or "").strip()
    return bool(v) and len(v) <= 12 and not re.search(r"[。？?!！,，;；]", v)


def detect_columns(rows: list) -> dict:
    """认出表头行与各列下标（不取值）。

    返回 {header, head_idx, prompt_col, scene_col, target_col, total_rows, preview}
    识别不出的列一律留 None —— 不硬套，界面会让用户自己选列。
    """
    rows = [list(r or []) for r in (rows or [])]
    while rows and not any((c or "").strip() for c in rows[-1]):
        rows.pop()                       # 去掉尾部整行空行
    if not rows:
        return {"header": [], "head_idx": None, "prompt_col": 0, "scene_col": None,
                "target_col": None, "total_rows": 0, "preview": []}

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

    def header_col(names, exclude=()):
        for i in range(width):
            text = ((header[i] if i < len(header) else "") or "").strip().lower()
            if not text or i in exclude:
                continue
            for nm in names:
                if nm.lower() in text:
                    return i
        return None

    prompt_col = header_col(PROMPT_HEADERS)
    scene_col = header_col(SCENE_HEADERS,
                           exclude=({prompt_col} if prompt_col is not None else set()))
    target_col = header_col(TARGET_HEADERS,
                            exclude={c for c in (prompt_col, scene_col) if c is not None})
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
    return {"header": [c or "" for c in header], "head_idx": head_idx,
            "prompt_col": prompt_col, "scene_col": scene_col, "target_col": target_col,
            "total_rows": len(body), "preview": body}


def rows_to_items(rows: list, head_idx, prompt_col: int,
                  scene_col=None, target_col=None) -> dict:
    """按给定列下标从数据行里取值（用户可在界面上改列）。

    返回 {items, skipped}：items 每项 {"prompt", "scene", "targets", "raw_target"}；
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
        raw_targets = get(target_col)
        # 只认白名单里的目标（与预设用例库的 labels.targets 口径一致）
        targets = [t for t in KNOWN_TARGETS
                   if re.search(rf"(?<![a-z]){t}(?![a-z])", raw_targets, re.I)]
        items.append({"prompt": _CELL_NL.sub("\n", prompt),
                      "scene": get(scene_col),
                      "targets": targets,
                      "raw_target": raw_targets})
    return {"items": items, "skipped": skipped}


def detect_table(rows: list) -> dict:
    """一步到位：认列 + 取值（供不便分两步的调用方使用）。"""
    meta = detect_columns(rows)
    got = rows_to_items(rows, meta["head_idx"], meta["prompt_col"],
                        meta["scene_col"], meta["target_col"])
    return {k: v for k, v in meta.items() if k != "preview"} | got
