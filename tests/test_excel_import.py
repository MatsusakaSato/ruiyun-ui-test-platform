import os
import unittest
import tempfile
from pathlib import Path

from core.xlsx_reader import detect_columns, infer_targets, rows_to_items, read_table
from core.testcase_db import init_db, add_preset_cases, get_preset_cases, count_cases


class TestExcelImport(unittest.TestCase):
    def test_detect_columns_benchmark(self):
        header = ['序号', '场景', '学科', '难度', '输入类型', '是否本Agent强项', 'Query内容', 'Skill', '附件']
        sample_row = ['1', '备课', '语文', '常规', '纯文本', '是', '写一篇教案', 'office-routing', '']
        col_info = detect_columns([header, sample_row])

        self.assertEqual(col_info['head_idx'], 0)
        self.assertEqual(col_info['name_col'], 0)         # '序号'
        self.assertEqual(col_info['scene_col'], 1)        # '场景'
        self.assertEqual(col_info['prompt_col'], 6)       # 'Query内容' (NOT '输入类型')
        self.assertEqual(col_info['skill_col'], 7)        # 'Skill'
        self.assertEqual(col_info['attachment_col'], 8)   # '附件'

    def test_infer_targets(self):
        # 1. Attachment extension priority
        self.assertEqual(infer_targets("随便写点什么", attachment_name="课程大纲.docx"), ["word"])
        self.assertEqual(infer_targets("请分析数据", attachment_name="销售数据.xlsx"), ["excel"])
        self.assertEqual(infer_targets("请讲解材料", attachment_name="课件.pptx"), ["ppt"])
        self.assertEqual(infer_targets("阅读材料", attachment_name="论文.pdf"), ["pdf"])

        # 2. Prompt keywords
        self.assertIn("ppt", infer_targets("请帮我制作一份精美的公开课课件PPT"))
        self.assertIn("excel", infer_targets("制作一份学生成绩统计表，包含平均分计算"))
        self.assertIn("word", infer_targets("生成一份高中语文教案，包含教学反思与板书设计"))

        # 3. Explicit raw_target override
        self.assertEqual(infer_targets("测试提问", raw_target="word, ppt"), ["word", "ppt"])

    def test_rows_to_items_parsing(self):
        header = ['序号', '场景', '学科', '难度', '输入类型', '是否本Agent强项', 'Query内容', 'Skill', '附件']
        rows = [
            header,
            ['1', '备课', '语文', '中等', '带附件', '是', '根据课文设计活动', 'office-routing', '参考课文.docx'],
            ['2', '出题', '数学', '困难', '纯文本', '否', '出三道函数大题', 'math-skill', ''],
            ['3', '出题', '英语', '简单', '纯文本', '否', '', '', ''],  # empty prompt, should be skipped
        ]
        col_info = detect_columns(rows)
        res = rows_to_items(
            rows,
            head_idx=col_info['head_idx'],
            prompt_col=col_info['prompt_col'],
            scene_col=col_info['scene_col'],
            target_col=col_info['target_col'],
            name_col=col_info['name_col'],
            attachment_col=col_info['attachment_col'],
            skill_col=col_info['skill_col'],
        )
        self.assertEqual(res['skipped'], 1)
        items = res['items']
        self.assertEqual(len(items), 2)

        # First item checks
        item1 = items[0]
        self.assertEqual(item1['name'], '备课-001')
        self.assertEqual(item1['scene'], '备课')
        self.assertEqual(item1['prompt'], '根据课文设计活动')
        self.assertEqual(item1['targets'], ['word'])
        self.assertTrue(item1['attachment'])
        self.assertEqual(item1['attachments'], ['参考课文.docx'])
        self.assertIn('office-routing', item1['expect_tools'])
        self.assertEqual(item1['labels']['subject'], '语文')
        self.assertEqual(item1['labels']['difficulty'], '中等')

        # Second item checks
        item2 = items[1]
        self.assertEqual(item2['name'], '出题-002')
        self.assertEqual(item2['scene'], '出题')
        self.assertFalse(item2['attachment'])
        self.assertEqual(item2['attachments'], [])

    def test_db_replace_and_append_mode(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "test_import.db"
            init_db(db_file)

            items_initial = [
                {'name': 'OLD-01', 'prompt': '旧问题1', 'scene': '旧场景', 'targets': ['word']},
                {'name': 'OLD-02', 'prompt': '旧问题2', 'scene': '旧场景', 'targets': ['excel']},
            ]
            add_preset_cases(items_initial, db_path=db_file)
            self.assertEqual(count_cases(db_file), 2)

            # Test Append
            items_append = [
                {'name': 'NEW-01', 'prompt': '新问题1', 'scene': '新场景', 'targets': ['ppt']},
            ]
            add_preset_cases(items_append, db_path=db_file, replace=False)
            self.assertEqual(count_cases(db_file), 3)

            # Test Replace
            items_replace = [
                {'name': 'REP-01', 'prompt': '覆盖问题1', 'scene': '覆盖场景', 'targets': ['pdf']},
            ]
            add_preset_cases(items_replace, db_path=db_file, replace=True)
            self.assertEqual(count_cases(db_file), 1)
            remaining = get_preset_cases(db_path=db_file)
            self.assertEqual(remaining[0]['name'], 'REP-01')
            self.assertEqual(remaining[0]['prompt'], '覆盖问题1')

    def test_actual_workspace_file_if_present(self):
        real_xlsx = Path("/Users/amano/WorkSpace/工作台测试集1000.xlsx")
        if not real_xlsx.exists():
            self.skipTest("Real xlsx not present")

        with open(real_xlsx, "rb") as f:
            data = f.read()

        table = read_table("工作台测试集1000.xlsx", data)
        self.assertEqual(len(table['rows']), 1001)

        cols = detect_columns(table['rows'])
        self.assertEqual(cols['prompt_col'], 6)
        self.assertEqual(cols['name_col'], 0)
        self.assertEqual(cols['attachment_col'], 8)

        res = rows_to_items(
            table['rows'],
            head_idx=cols['head_idx'],
            prompt_col=cols['prompt_col'],
            scene_col=cols['scene_col'],
            name_col=cols['name_col'],
            attachment_col=cols['attachment_col'],
            skill_col=cols['skill_col'],
        )
        self.assertEqual(len(res['items']), 1000)
        self.assertEqual(res['skipped'], 0)


if __name__ == '__main__':
    unittest.main()
