"""本地 SQLite 预设用例数据库 (testcases.db) 单元测试集。"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.testcase_db import (
    init_db,
    query_preset_cases,
    add_preset_case,
    add_preset_cases,
    delete_preset_cases,
    get_preset_cases,
    count_cases,
    get_preset_labels_index,
)
from run_pipeline import load_cases


class TestTestCaseDB(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "testcases.db"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_db_creates_tables_and_indexes(self):
        """测试 init_db 成功创建数据表与索引。"""
        init_db(self.db_path)
        self.assertTrue(self.db_path.is_file())

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 检查表结构
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='testcases';")
            self.assertIsNotNone(cursor.fetchone())

            cursor.execute("PRAGMA table_info(testcases);")
            cols = {row[1]: row[2] for row in cursor.fetchall()}
            self.assertIn("id", cols)
            self.assertIn("name", cols)
            self.assertIn("prompt", cols)
            self.assertIn("scene", cols)
            self.assertIn("targets", cols)
            self.assertIn("attachment", cols)
            self.assertIn("expect_tools", cols)
            self.assertIn("attachments", cols)
            self.assertIn("seq", cols)

            # 检查索引
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='testcases';")
            indexes = {row[0] for row in cursor.fetchall()}
            self.assertIn("idx_testcases_seq", indexes)
            self.assertIn("idx_testcases_scene", indexes)
            self.assertIn("idx_testcases_attachment", indexes)

    def test_add_and_query_preset_cases(self):
        """测试增加、分页、模糊搜索与标签过滤。"""
        init_db(self.db_path)

        # 批量添加
        new_items = [
            {
                "prompt": "帮我整理一份财务报表",
                "name": "财务分析",
                "scene": "办公",
                "targets": ["excel"],
            },
            {
                "prompt": "设计一份教案并导出PDF",
                "name": "数学教案",
                "scene": "教学",
                "targets": ["pdf", "word"],
                "attachments": ["math.txt"],
            },
            {
                "prompt": "网页内容爬取分析",
                "name": "爬虫任务",
                "scene": "技术",
                "targets": ["html", "network"],
            },
        ]
        ok, msg, count = add_preset_cases(new_items, db_path=self.db_path)
        self.assertTrue(ok)
        self.assertEqual(count, 3)

        # 单条添加
        ok_single, msg_single, single = add_preset_case(
            {"prompt": "第四个任务", "scene": "教学", "targets": ["ppt"]},
            db_path=self.db_path
        )
        self.assertTrue(ok_single)
        self.assertEqual(single["id"], "CASE-004")

        # 检查总数
        self.assertEqual(count_cases(self.db_path), 4)

        # 分页查询 (order="asc")
        res_page = query_preset_cases(db_path=self.db_path, offset=0, limit=2, order="asc")
        self.assertEqual(res_page["total"], 4)
        self.assertEqual(len(res_page["cases"]), 2)
        self.assertEqual(res_page["cases"][0]["id"], "CASE-001")
        self.assertEqual(res_page["cases"][1]["id"], "CASE-002")

        # 关键词模糊搜索
        res_search = query_preset_cases(db_path=self.db_path, keyword="教案")
        self.assertEqual(res_search["total"], 1)
        self.assertEqual(res_search["cases"][0]["id"], "CASE-002")

        # 按场景过滤
        res_scene = query_preset_cases(db_path=self.db_path, scene="教学")
        self.assertEqual(res_scene["total"], 2)
        ids = [item["id"] for item in res_scene["cases"]]
        self.assertIn("CASE-002", ids)
        self.assertIn("CASE-004", ids)

        # 按目标过滤 (测试 SQLite json_each)
        res_target = query_preset_cases(db_path=self.db_path, targets=["pdf"])
        self.assertEqual(res_target["total"], 1)
        self.assertEqual(res_target["cases"][0]["id"], "CASE-002")

        # 按附件过滤
        res_attach = query_preset_cases(db_path=self.db_path, attachment="yes")
        self.assertEqual(res_attach["total"], 1)
        self.assertEqual(res_attach["cases"][0]["id"], "CASE-002")

    def test_delete_preset_cases(self):
        """测试删除预设用例。"""
        init_db(self.db_path)
        add_preset_cases([
            {"id": "CASE-001", "prompt": "1"},
            {"id": "CASE-002", "prompt": "2"},
            {"id": "CASE-003", "prompt": "3"},
        ], db_path=self.db_path)

        # 删除单个
        ok, msg, deleted = delete_preset_cases(["CASE-002"], db_path=self.db_path)
        self.assertTrue(ok)
        self.assertEqual(deleted, 1)
        self.assertEqual(count_cases(self.db_path), 2)

        # 删除不存在的
        ok_none, msg_none, deleted_none = delete_preset_cases(["CASE-999"], db_path=self.db_path)
        self.assertFalse(ok_none)
        self.assertEqual(deleted_none, 0)

        # 批量删除
        ok_batch, msg_batch, deleted_batch = delete_preset_cases(["CASE-001", "CASE-003"], db_path=self.db_path)
        self.assertTrue(ok_batch)
        self.assertEqual(deleted_batch, 2)
        self.assertEqual(count_cases(self.db_path), 0)

    def test_get_preset_labels_index(self):
        """测试获取基于 prompt 归一化的标签索引字典。"""
        init_db(self.db_path)
        add_preset_cases([
            {"id": "CASE-001", "prompt": "制作 PPT 课件", "labels": {"scene": "教学", "targets": ["ppt"]}},
            {"id": "CASE-002", "prompt": "生成 报告 方案", "labels": {"scene": "办公", "targets": ["word"]}},
        ], db_path=self.db_path)

        idx = get_preset_labels_index(self.db_path)
        self.assertEqual(idx["制作PPT课件"], {"scene": "教学", "targets": ["ppt"]})
        self.assertEqual(idx["生成报告方案"], {"scene": "办公", "targets": ["word"]})

    def test_load_cases_from_sqlite(self):
        """测试 run_pipeline.load_cases 支持直接从 SQLite 数据库加载。"""
        init_db(self.db_path)
        add_preset_cases([
            {"id": "CASE-001", "prompt": "测试用例 1", "labels": {"scene": "教学", "targets": ["ppt"]}},
            {"id": "CASE-002", "prompt": "测试用例 2", "labels": {"scene": "办公", "targets": ["excel"]}},
        ], db_path=self.db_path)

        loaded = load_cases(str(self.db_path))
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "CASE-001")
        self.assertEqual(loaded[0]["prompt"], "测试用例 1")
        self.assertEqual(loaded[0]["labels"]["scene"], "教学")
        self.assertEqual(loaded[0]["labels"]["targets"], ["ppt"])

        self.assertEqual(loaded[1]["id"], "CASE-002")
        self.assertEqual(loaded[1]["labels"]["targets"], ["excel"])


if __name__ == "__main__":
    unittest.main()
