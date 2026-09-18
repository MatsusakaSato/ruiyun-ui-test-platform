"""从问题原文推断用例意图：需要的产物类型 与 教学/非教学分组。

为什么需要它：界面手输的用例没有 labels，而且其 id 是按序号自动生成的
（`CASE-001…`），与 `testcases.yaml` 的预设 id **必然重名**。因此评估绝不能
「按同名 id 借预设标签」——那会把预设的 `targets: ["word"]` 凭空安到无关问题上，
曾导致「根本没有产物要求，却被判产物未产出」得 0 分。

本模块**只使用原文中的显式证据**，并返回命中的证据词；调用方据此把来源标注为
「声明」还是「推断」，让结论可复核、可反驳。

纯函数、无 I/O，供 core/evaluator.py 使用，也可被界面复用。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 产物类型推断
# 产出动词：必须与类型词出现在**同一分句**，才认为「要求交付该类型文件」。
# 这样「分析这个表格里的数据」不会被误判成「要求产出 Excel」。
_PRODUCE_VERBS = (
    "生成", "输出", "导出", "做成", "整理成", "汇总成", "形成", "转成", "另存为",
    "写一份", "写个", "写一份", "给一份", "给我一份", "提供一份", "出一份", "发一份",
    "做一个", "制作", "创建", "下载", "整理一份", "列一份", "整理出",
)

# 类型词 → kind（取值与 core/artifacts.EXT_KINDS 一致）
_KIND_WORDS = (
    ("pptx", ("pptx", "ppt", "幻灯片", "演示文稿", "课件")),
    ("excel", ("xlsx", "excel", "xls", "csv", "电子表格", "表格文件", "表格")),
    ("docx", ("docx", ".doc", "word", "文档", "文稿")),
    ("pdf", ("pdf",)),
    ("html", ("html", "网页", "网站")),
)

# 分句：产物要求通常是「动词 + 类型」同句出现，按子句切开可显著降低误判
_CLAUSE = re.compile(r"[。！？；;!?\n，,、]+")


def infer_target_kinds(prompt: str) -> tuple:
    """从问题原文推断需要交付的产物类型。

    返回 (kinds, hits)：kinds 为 kinds 集合（无显式证据则为空集），
    hits 为命中的证据短语（如「生成…word」），供界面标注推断依据。
    """
    text = str(prompt or "").strip().lower()
    if not text:
        return set(), []
    kinds: set = set()
    hits: list = []
    for clause in _CLAUSE.split(text):
        if not clause.strip():
            continue
        verb = next((v for v in _PRODUCE_VERBS if v in clause), "")
        if not verb:
            continue                      # 没有产出动词 → 不作为「要求交付」
        for kind, words in _KIND_WORDS:
            word = next((w for w in words if w in clause), "")
            if word and kind not in kinds:
                kinds.add(kind)
                hits.append(f"{verb}…{word}")
    return kinds, hits


# ---------------------------------------------------------------- 场景推断
# 教学场景（按优先级命中）。words=关键词；patterns=结构式表达（年级/学段等，
# 穷举不现实故交给正则），命中任一即采用该场景。
# 取值必须落在 core.eval_rubric.TEACHING_SCENES 内。
_SCENE_RULES = (
    ("家校沟通", ("家长", "家校", "家访", "家长会", "班主任", "监护人"), ()),
    ("作业批改", ("作业", "批改", "评语", "错题", "订正", "试卷", "练习题", "批阅", "命题"), ()),
    ("备课", ("教学设计", "教案", "备课", "课时", "板书", "教学目标", "教学过程",
              "学情", "导学案", "说课", "课件", "单元设计", "复习课", "新课导入"), ()),
    ("教育-其他",
     ("教育", "教学", "教师", "学生", "学校", "课堂", "教研", "课题", "教材",
      "年级", "统编版", "学科", "学业", "成绩", "考试", "课程", "学段",
      "知识点", "班级", "论文", "微课", "教研组", "学籍", "团员", "团支部",
      "少先队", "中考", "高考", "升学", "毕业", "幼儿园", "备课组"),
     (re.compile(r"[高初][一二三]"),           # 高三 / 初二
      re.compile(r"[一二三四五六七八九]年级"),  # 三年级
      re.compile(r"小[一二三四五六]|大班|中班|小班"))),
)

# 明确的非教学领域信号：命中即判为非教学（避免把所有未知问题都塞进教学组）
_NON_TEACHING_WORDS = (
    "合同", "发票", "报销", "财务报表", "税法", "法律", "诉讼", "简历", "招聘",
    "营销", "广告", "代码", "编程", "sql", "接口", "服务器", "运维", "数据库",
    "部署", "周报", "kpi", "旅游", "菜谱", "健身", "股票", "基金", "保险", "房产",
    "产品需求", "项目管理", "客服",
)


def infer_scene(prompt: str) -> tuple:
    """从问题原文推断场景标签。返回 (scene, hits)。

    scene 为空字符串表示**无法判断** → 调用方保持「未分类」（教学组与结果质量组
    都不评），不做臆测；非教学问题返回 "非教学"。
    """
    text = str(prompt or "").strip().lower()
    if not text:
        return "", []
    for scene, words, pats in _SCENE_RULES:
        hit = [w for w in words if w in text]
        # 证据保留「实际命中的文本」（如「高三」），而不是正则本身
        hit += [m.group(0) for m in (p.search(text) for p in pats) if m]
        if hit:
            return scene, hit[:3]
    hit = [w for w in _NON_TEACHING_WORDS if w in text]
    if hit:
        return "非教学", hit[:3]
    return "", []
