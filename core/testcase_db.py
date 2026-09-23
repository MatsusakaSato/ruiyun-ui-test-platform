"""本地 SQLite 测试集数据库管理模块。

预设用例库存储于本地 SQLite 数据库（testcases.db），
基于 Python 标准库 sqlite3 实现，提供高性能分页、多维标签组合筛选、
原子事务增删与全量索引支持。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from core.settings import preset_path


def get_db_path(db_path: Path | str | None = None) -> Path:
    """获取测试集数据库文件路径。"""
    if db_path is not None:
        p = Path(db_path).expanduser().resolve()
    else:
        p = preset_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """创建并配置 SQLite 连接。"""
    path = get_db_path(db_path)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    # 启用 WAL 模式提高读写并发与稳定性
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn


def _case_seq(case_id: str) -> int:
    """提取 CASE-NNN 里的数字序号，用于自然排序和最大序列计算。"""
    m = re.match(r"^CASE-(\d+)$", str(case_id or "").strip(), flags=re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _norm_labels(raw_labels: Any) -> dict:
    """将用例标签规整化为三维度字典。"""
    if not isinstance(raw_labels, dict):
        raw_labels = {}
    scene = str(raw_labels.get("scene") or "").strip()
    targets = [str(t).strip() for t in (raw_labels.get("targets") or []) if str(t).strip()]
    attachment = bool(raw_labels.get("attachment"))
    out: dict = {}
    if scene:
        out["scene"] = scene
    if targets:
        out["targets"] = targets
    if attachment:
        out["attachment"] = True
    return out


def _row_to_case(row: sqlite3.Row) -> dict:
    """将数据库行转换为上层接口一致的字典结构。"""
    targets = []
    if row["targets"]:
        try:
            targets = json.loads(row["targets"])
        except Exception:
            targets = []

    expect_tools = []
    if row["expect_tools"]:
        try:
            expect_tools = json.loads(row["expect_tools"])
        except Exception:
            expect_tools = []

    attachments = []
    if row["attachments"]:
        try:
            attachments = json.loads(row["attachments"])
        except Exception:
            attachments = []

    labels: dict = {}
    if row["scene"]:
        labels["scene"] = row["scene"]
    if targets:
        labels["targets"] = targets
    if bool(row["attachment"]):
        labels["attachment"] = True

    res = {
        "id": row["id"],
        "name": row["name"] or "",
        "prompt": row["prompt"] or "",
        "expect_tools": expect_tools,
        "labels": labels,
        "attachments": attachments,
    }
    if attachments:
        res["attachment"] = attachments[0]
    return res


def init_db(db_path: Path | str | None = None) -> Path:
    """初始化数据库表与索引。"""
    path = get_db_path(db_path)
    with _get_connection(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS testcases (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL,
                scene TEXT NOT NULL DEFAULT '',
                targets TEXT NOT NULL DEFAULT '[]',
                attachment INTEGER NOT NULL DEFAULT 0,
                expect_tools TEXT NOT NULL DEFAULT '[]',
                attachments TEXT NOT NULL DEFAULT '[]',
                seq INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_testcases_seq ON testcases(seq);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_testcases_scene ON testcases(scene);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_testcases_attachment ON testcases(attachment);")
        conn.commit()

    return path


def count_cases(db_path: Path | str | None = None) -> int:
    """获取预设库中的用例总数。"""
    init_db(db_path)
    with _get_connection(db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM testcases")
        return int(cur.fetchone()[0])


def get_preset_cases(db_path: Path | str | None = None) -> list[dict]:
    """读取预设用例库中的全部用例，按序号升序排列。"""
    init_db(db_path)
    with _get_connection(db_path) as conn:
        cur = conn.execute("SELECT * FROM testcases ORDER BY seq ASC, id ASC")
        return [_row_to_case(row) for row in cur.fetchall()]


def query_preset_cases(keyword: str = "", scene: str = "", targets=None,
                        attachment: str = "", ids=None, limit: int = 50,
                        offset: int = 0, order: str = "desc",
                        db_path: Path | str | None = None) -> dict:
    """预设用例库分页与条件筛选查询。

    keyword 匹配 id / name / prompt；
    scene 精确匹配场景标签；
    targets 匹配测试目标包含项；
    attachment 匹配 'yes' (必须附件) 或 'no' (无附件)；
    ids 用于精确提取指定 ID 的一批用例。
    返回 {cases, total, offset, limit, scenes, targets}。
    """
    init_db(db_path)
    with _get_connection(db_path) as conn:
        # 1. 统计整个库的全局筛选项（不受当前筛选条件影响）
        cur_sc = conn.execute("SELECT DISTINCT scene FROM testcases WHERE scene != '' ORDER BY scene")
        scenes = [r[0] for r in cur_sc.fetchall()]

        all_targets: list[str] = []
        try:
            cur_tg = conn.execute("""
                SELECT DISTINCT value FROM testcases, json_each(testcases.targets)
                WHERE value != '' ORDER BY value
            """)
            all_targets = [r[0] for r in cur_tg.fetchall()]
        except Exception:
            pass

        # 2. 构建动态查询条件
        where_clauses = ["1=1"]
        params: list[Any] = []

        idset = [str(i).strip() for i in (ids or []) if str(i).strip()]
        if idset:
            placeholders = ",".join(["?"] * len(idset))
            where_clauses.append(f"id IN ({placeholders})")
            params.extend(idset)

        kw = str(keyword or "").strip().lower()
        if kw:
            where_clauses.append("(LOWER(id) LIKE ? OR LOWER(name) LIKE ? OR LOWER(prompt) LIKE ?)")
            kw_param = f"%{kw}%"
            params.extend([kw_param, kw_param, kw_param])

        sc = str(scene or "").strip()
        if sc:
            where_clauses.append("scene = ?")
            params.append(sc)

        want_targets = [str(t).strip() for t in (targets or []) if str(t).strip()]
        if want_targets:
            tg_placeholders = ",".join(["?"] * len(want_targets))
            where_clauses.append(f"""
                EXISTS (
                    SELECT 1 FROM json_each(testcases.targets)
                    WHERE value IN ({tg_placeholders})
                )
            """)
            params.extend(want_targets)

        if attachment == "yes":
            where_clauses.append("attachment = 1")
        elif attachment == "no":
            where_clauses.append("attachment = 0")

        where_sql = " AND ".join(where_clauses)

        # 3. 统计符合条件的总行数
        cur_total = conn.execute(f"SELECT COUNT(*) FROM testcases WHERE {where_sql}", params)
        total = int(cur_total.fetchone()[0])

        # 4. 获取分页记录
        order_dir = "ASC" if str(order).lower() == "asc" else "DESC"
        limit_val = max(1, min(int(limit or 50), 500))
        offset_val = max(0, int(offset or 0))

        page_sql = f"""
            SELECT * FROM testcases
            WHERE {where_sql}
            ORDER BY seq {order_dir}, id {order_dir}
            LIMIT ? OFFSET ?
        """
        page_params = list(params) + [limit_val, offset_val]
        cur_rows = conn.execute(page_sql, page_params)
        cases = [_row_to_case(r) for r in cur_rows.fetchall()]

        return {
            "cases": cases,
            "total": total,
            "offset": offset_val,
            "limit": limit_val,
            "scenes": scenes,
            "targets": all_targets,
        }


def add_preset_case(payload: dict, db_path: Path | str | None = None) -> tuple[bool, str, dict | None]:
    """新增单条预设用例，id 自动取当前最大 CASE-NNN 顺延。"""
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return False, "提问为必填", None

    init_db(db_path)
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, dict):
        raw_labels = {
            "scene": payload.get("scene") or "",
            "targets": payload.get("targets") or [],
            "attachment": bool(payload.get("attachment") or payload.get("attachments")),
        }
    labels = _norm_labels(raw_labels)
    scene = labels.get("scene", "")
    targets = json.dumps(labels.get("targets", []), ensure_ascii=False)
    attachment = 1 if (labels.get("attachment") or payload.get("attachment") or payload.get("attachments")) else 0
    expect_tools = json.dumps(
        [str(t).strip() for t in (payload.get("expect_tools") or []) if str(t).strip()],
        ensure_ascii=False
    )
    attachments = json.dumps(
        [str(p).strip() for p in (payload.get("attachments") or []) if str(p).strip()],
        ensure_ascii=False
    )

    with _get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM testcases")
        max_seq = int(cur.fetchone()[0])
        seq = max_seq + 1
        cid = f"CASE-{seq:03d}"
        name = str(payload.get("name") or "").strip() or f"自定义-{seq:03d}"

        cur.execute("""
            INSERT INTO testcases
            (id, name, prompt, scene, targets, attachment, expect_tools, attachments, seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cid, name, prompt, scene, targets, attachment, expect_tools, attachments, seq))
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM testcases")
        total = int(cur.fetchone()[0])

    new_case = {
        "id": cid,
        "name": name,
        "prompt": prompt,
        "expect_tools": json.loads(expect_tools),
        "labels": labels,
        "attachments": json.loads(attachments),
    }
    return True, f"已新增 {cid}（预设共 {total} 条）", new_case


def add_preset_cases(items: list, db_path: Path | str | None = None,
                     replace: bool = False) -> tuple[bool, str, int]:
    """批量新增或覆盖预设用例（Excel / CSV 导入），在单一事务内完成。

    replace=True: 清空已有预设用例，以传入列表重新建库；
    replace=False: 追加到现有预设用例库末尾，序号顺延。
    """
    rows = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        prompt = str(it.get("prompt") or "").strip()
        if not prompt:
            continue
        raw_labels = it.get("labels")
        if not isinstance(raw_labels, dict):
            raw_labels = {
                "scene": it.get("scene") or "",
                "targets": it.get("targets") or [],
                "attachment": bool(it.get("attachment") or it.get("attachments")),
            }
        labels = _norm_labels(raw_labels)
        has_att = bool(labels.get("attachment") or it.get("attachment") or it.get("attachments"))
        if has_att:
            labels["attachment"] = True

        rows.append({
            "id": it.get("id"),
            "prompt": prompt,
            "name": str(it.get("name") or "").strip(),
            "labels": labels,
            "expect_tools": it.get("expect_tools") or [],
            "attachments": it.get("attachments") or ([it["attachment"]] if it.get("attachment") else []),
        })

    if not rows:
        return False, "没有可导入的用例（提问列全为空？）", 0

    init_db(db_path)
    with _get_connection(db_path) as conn:
        cur = conn.cursor()
        if replace:
            cur.execute("DELETE FROM testcases")
            seq = 0
        else:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM testcases")
            seq = int(cur.fetchone()[0])

        insert_data = []
        for it in rows:
            seq += 1
            cid = it.get("id") if (replace and it.get("id")) else f"CASE-{seq:03d}"
            name = it["name"] or f"导入-{seq:03d}"
            scene = it["labels"].get("scene", "")
            targets = json.dumps(it["labels"].get("targets", []), ensure_ascii=False)
            attachment = 1 if (it["labels"].get("attachment") or it["attachments"]) else 0
            expect_tools = json.dumps(it["expect_tools"], ensure_ascii=False)
            attachments = json.dumps(it["attachments"], ensure_ascii=False)

            insert_data.append((
                cid, name, it["prompt"], scene, targets, attachment,
                expect_tools, attachments, seq
            ))

        cur.executemany("""
            INSERT OR REPLACE INTO testcases
            (id, name, prompt, scene, targets, attachment, expect_tools, attachments, seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, insert_data)
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM testcases")
        total = int(cur.fetchone()[0])

    mode_text = "覆盖" if replace else "新增"
    return True, f"已{mode_text}导入 {len(rows)} 条（预设共 {total} 条）", len(rows)


def delete_preset_cases(ids: list | set, db_path: Path | str | None = None) -> tuple[bool, str, int]:
    """按 ID 批量删除预设用例。"""
    idset = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not idset:
        return False, "未指定要删除的用例 id", 0

    init_db(db_path)
    with _get_connection(db_path) as conn:
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(idset))
        cur.execute(f"DELETE FROM testcases WHERE id IN ({placeholders})", idset)
        removed = cur.rowcount
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM testcases")
        remaining = int(cur.fetchone()[0])

    if removed == 0:
        return False, "没有匹配到要删除的预设用例（可能已被删除）", 0

    return True, f"已删除 {removed} 条（预设剩余 {remaining} 条）", removed


def get_preset_labels_index(db_path: Path | str | None = None) -> dict[str, dict]:
    """生成评估器专用索引：normalize(prompt) -> labels。"""
    init_db(db_path)
    out: dict[str, dict] = {}
    with _get_connection(db_path) as conn:
        cur = conn.execute("SELECT prompt, scene, targets, attachment FROM testcases")
        for row in cur.fetchall():
            prompt = row["prompt"] or ""
            key = re.sub(r"\s+", "", prompt)
            if not key:
                continue

            labels: dict = {}
            if row["scene"]:
                labels["scene"] = row["scene"]
            if row["targets"]:
                try:
                    tg = json.loads(row["targets"])
                    if tg:
                        labels["targets"] = tg
                except Exception:
                    pass
            if bool(row["attachment"]):
                labels["attachment"] = True

            out[key] = labels
    return out
