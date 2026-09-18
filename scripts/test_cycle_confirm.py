#!/usr/bin/env python3
"""注入式测试：选项卡事实驱动推进 / 视图锁定 / 止损 / 巡检护栏。

用假 CDP 模拟应用 DOM（sidebar 列表 + agent-question-composer 卡片），
不驱动真实应用。全部断言通过即 PASS。覆盖的失效场景均为实测过的：
  * 切走会话视图会重置卡片 → 巡检必须锁定视图（probe_qcard.py --switch-test 实证）
  * 提交中按钮禁用 → 旧实现仍记「成功点击」，制造假事件洪水直至熔断
  * 同一下载/同一指纹点不动 → 必须几秒内转人工，而不是点满 confirm_max_clicks
"""
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivers.ui_driver import RuiyunUIDriver  # noqa: E402

CFG = {"app": {"binary": "/fake/App", "debug_port": 9222, "auto_confirm": True,
               "confirm_keywords": ["允许", "确认"],
               "confirm_cooldown_s": 0, "confirm_max_clicks": 200,
               "auto_confirm_cycle_s": 6,
               # 测试里把节流关掉，才能在一轮内连续验证尝试计数
               "qcard_min_click_interval_s": 0,
               "qcard_same_card_limit": 6},
       "paths": {"session_root": str(Path(tempfile.gettempdir()) / "fake_sessions")}}

ITEMS = [
    {"idx": 0, "title": "你好...", "time": "5分钟前", "unread": False,
     "status": "other", "visible": True},
    {"idx": 1, "title": "你好...", "time": "6分钟前", "unread": True,
     "status": "running", "visible": True},
    {"idx": 2, "title": "为统编版三年级上册《大青树下的...", "time": "7分钟前",
     "unread": False, "status": "running", "visible": True},
]

PRIMARY_SUBMIT = {"kind": "primary", "text": "提交", "disabled": False, "visible": True}
PRIMARY_CONFIRM = {"kind": "primary", "text": "确认", "disabled": False, "visible": True}
SKIP = {"kind": "skip", "text": "跳过", "disabled": False, "visible": True}
PRIMARY_DISABLED = {"kind": "primary", "text": "提交中…", "disabled": True, "visible": True}
NOT_FOUND = {"found": False}


def card(counter="1/3", question="这份总结给谁看？", options=("自用", "给同事"),
         primary=None, skip=SKIP, selected=(), custom=None, submitting=False,
         is_last=False, options_disabled=False):
    """构造一份与真实探测 JS 同构的卡片事实。"""
    return {
        "found": True, "counter": counter, "question": question,
        "questionIndex": 0, "questionCount": 3, "isLast": is_last,
        "options": [{"label": o, "selected": o in selected,
                     "disabled": options_disabled, "visible": True} for o in options],
        "customInput": custom, "inputDisabled": False,
        "submitting": submitting, "primary": primary, "skip": skip,
    }


class FakeCaseResult:
    def __init__(self, prompt):
        self.prompt = prompt


class FakeCDP:
    """按表达式特征路由的假 CDP；facts 用尽后重复最后一份。"""

    def __init__(self, facts=None, generic=None, sidebar_ok=True):
        self.facts = list(facts or [NOT_FOUND])
        self.idx = 0
        self.generic = generic
        self.sidebar_ok = sidebar_ok
        self.clicks = []          # 被点击的目标类型序列
        self.opens = []           # 打开过的会话序号
        self.click_results = {}   # 目标类型 → 点击返回

    def _next_facts(self):
        f = self.facts[min(self.idx, len(self.facts) - 1)]
        self.idx += 1
        return dict(f)

    def eval_js(self, expr, timeout=10):
        # 路由顺序很关键：三类 JS 互相包含对方的标志串
        #   ① 点击 JS 独有 'no-card' 分支
        #   ② 通用两层检测模板独有 prefer/deny（它现在也含 "agent-question" 排除词）
        #   ③ 选项卡探测 JS 独有 agent-question-composer
        if "reason: 'no-card'" in expr:
            for kind in ("fill-other", "option", "primary"):
                if f'"{kind}"' in expr:      # 只认 json.dumps 出来的双引号实参
                    self.clicks.append(kind)
                    return self.click_results.get(kind, {"ok": True, "kind": kind, "text": "x"})
            self.clicks.append("?")
            return {"ok": False, "reason": "unknown-kind"}
        if "prefer" in expr:
            return self.generic
        if "agent-question-composer" in expr:
            return self._next_facts()
        if "session-sidebar-task-item__button" in expr and "map(" in expr:
            return list(ITEMS) if self.sidebar_ok else None
        if "task-item')[idx]" in expr:
            self.opens.append(expr.strip().splitlines()[-2].strip())
            return "clicked"
        return self.generic


def make_driver(facts=None, **kw):
    drv = RuiyunUIDriver(CFG)
    drv.cdp = FakeCDP(facts=facts, **kw)
    return drv


def sess(name):
    return Path(tempfile.gettempdir()) / "fake_sessions" / name


# --------------------------------------------------------------------- 测试
def test_find_item():
    drv = make_driver()
    it = drv._find_item(ITEMS, "为统编版三年级上册《大青树下的小学》生成教学设计")
    assert it["idx"] == 2, it
    it = drv._find_item(ITEMS, "你好")
    assert it["unread"] is True and it["idx"] == 1, it
    drv._cycle_visited.add(("你好...", "5分钟前"))
    it = drv._find_item(ITEMS, "你好")
    assert it["idx"] == 1, it
    print("PASS 1: 标题匹配 / 未读优先 / 轮转去重")


def test_fact_driven_progress():
    """未作答→点选项；已作答→点 primary；末题 primary=提交，非末题=确认。"""
    # ① 非末题未作答（底部是「跳过」）→ 只能点选项，绝不点 primary
    drv = make_driver([card()])
    ev = drv._qcard_handle()
    assert ev and ev["mode"] == "qcard-option", ev
    assert drv.cdp.clicks == ["option"], drv.cdp.clicks
    # ② 非末题已作答（底部变「确认」）→ 点 primary = 确认进入下一题
    drv = make_driver([card(selected=("自用",), primary=PRIMARY_CONFIRM,
                            options=("自用", "给同事"))])
    ev = drv._qcard_handle()
    assert ev and ev["mode"] == "qcard-confirm", ev
    assert drv.cdp.clicks == ["primary"], drv.cdp.clicks
    # ③ 末题已作答 → 提交
    drv = make_driver([card(counter="3/3", is_last=True, selected=("自用",),
                            primary=PRIMARY_SUBMIT)])
    ev = drv._qcard_handle()
    assert ev and ev["mode"] == "qcard-submit", ev
    # 事件留痕：旧字段齐全（dashboard / run_pipeline 依赖），并带会话归属字段 sess
    for k in ("time", "ts", "mode", "text", "cls", "sess"):
        assert k in ev, ev
    assert ev["sess"] == "", "未设置当前会话时归属为空串，而不是缺字段"
    assert drv.confirm_events and drv.confirm_events[-1] == ev
    print("PASS 2: 未作答点选项 / 已作答点 primary（确认·提交）/ 事件字段完整")


def test_disabled_and_submitting_never_clicked():
    """提交中（按钮禁用）：不点击、不记事件；点击被拒时也不记成功事件。"""
    drv = make_driver([card(selected=("自用",), primary=PRIMARY_DISABLED,
                            submitting=True, options_disabled=True)])
    assert drv._qcard_handle() is None
    assert drv.cdp.clicks == [], drv.cdp.clicks
    assert drv.confirm_events == [], drv.confirm_events
    # 点击被拒（disabled）→ 返回 None、不记事件，但计入尝试（用于止损）
    drv = make_driver([card()])
    drv.cdp.click_results = {"option": {"ok": False, "reason": "disabled"}}
    assert drv._qcard_handle() is None
    assert drv.confirm_events == [], drv.confirm_events
    assert sum(drv._qcard_fp_clicks.values()) == 1, drv._qcard_fp_clicks
    print("PASS 3: 提交中不点击不记事件 / 点击未生效不记成功")


def test_no_progress_stops_early():
    """点下去但指纹永不变（如卡片已死）→ 几秒内转人工，绝不点满 confirm_max_clicks。"""
    drv = make_driver([card()])          # 每次都返回同一份事实 = 零推进
    for _ in range(20):
        drv._qcard_handle()
        if drv.confirm_human_needed:
            break
    assert drv.confirm_human_needed is True, "无进展必须转人工"
    assert drv.auto_confirm is False, "转人工同时应停用自动确认"
    assert len(drv.confirm_events) <= 6, f"应远少于 200 次熔断：{len(drv.confirm_events)}"
    print(f"PASS 4: 零推进 {len(drv.confirm_events)} 次尝试即转人工"
          f"（confirm_max_clicks=200 未被用满）")


def test_fill_free_text_question():
    """纯自由输入题（无选项）：填一句中性回答，而不是无声挂死。"""
    drv = make_driver([card(options=(), primary=PRIMARY_SUBMIT, skip=None, custom="")])
    ev = drv._qcard_handle()
    assert ev and ev["mode"] == "qcard-fill", ev
    assert drv.cdp.clicks == ["fill-other"], drv.cdp.clicks
    print("PASS 5: 无选项自由输入题 → 自动填写中性答案（qcard-fill）")


def test_card_reset_on_disappear():
    """卡片消失：进度状态复位、会话锁定解除。"""
    drv = make_driver([card()])
    drv._qcard_handle()
    assert drv._qcard_card_clicks == 1
    drv.cdp.facts = [NOT_FOUND]
    drv.cdp.idx = 0
    assert drv._qcard_handle() is None
    assert drv._qcard_card_clicks == 0 and drv._qcard_owner_sess is None
    print("PASS 6: 卡片消失 → 进度复位 + 解除会话锁定")


def test_view_lock_prevents_switching():
    """选项卡独占视图：发现卡片后本轮不再切换会话；次轮走机内推进不打开任何会话。"""
    drv = make_driver([NOT_FOUND, card()])       # 首次探测无卡 → 巡检；打开后出现卡片
    pairs = [(sess("a"), FakeCaseResult("你好")), (sess("b"), FakeCaseResult("明天提醒我喝水"))]
    remaining = {sess("a"), sess("b")}
    n = drv.cycle_and_confirm(pairs, remaining)
    assert n == 1, n
    assert drv._qcard_owner_sess == sess("a"), drv._qcard_owner_sess
    assert len(drv.cdp.opens) == 1, f"发现卡片后不应继续切会话：{drv.cdp.opens}"
    # 第二轮：当前视图仍有卡片 → 直接机内推进，一个会话都不打开
    opens_before = len(drv.cdp.opens)
    drv.cycle_and_confirm(pairs, remaining)
    assert len(drv.cdp.opens) == opens_before, "锁定期内不得再切换视图"
    print("PASS 7: 选项卡独占视图（发现卡片即锁定，不再切走 → 答案不被重置）")


def test_degrade_on_broken_sidebar():
    drv = make_driver([NOT_FOUND])
    drv.cdp.sidebar_ok = False
    pairs = [(sess("a"), FakeCaseResult("你好"))]
    for _ in range(3):
        assert drv.cycle_and_confirm(pairs, {sess("a")}) == 0
    assert drv.view_cycle_disabled is True, "连续 3 轮失败应停用巡检"
    drv.cdp.sidebar_ok = True
    assert drv.cycle_and_confirm(pairs, {sess("a")}) == 0
    print("PASS 8: sidebar 结构失效 → 3 轮降级停用，不再触发")


def test_skip_settled():
    drv = make_driver([NOT_FOUND])
    called = []
    drv._sidebar_items = lambda: called.append(1) or []
    pairs = [(sess("a"), FakeCaseResult("你好"))]
    assert drv.cycle_and_confirm(pairs, set()) == 0
    assert not called, "已判稳会话不应进入巡检"
    print("PASS 9: 已判稳会话跳过巡检")


def test_disabled_flag():
    drv = make_driver([card()])
    drv.view_cycle_disabled = True
    assert drv.cycle_and_confirm([(sess("a"), FakeCaseResult("你好"))], {sess("a")}) == 0
    assert drv.cdp.clicks == [], "停用巡检后不应有任何点击"
    print("PASS 10: view_cycle_disabled 时直接短路")


def test_generic_excludes_qcard_subtree():
    """通用两层检测必须排除选项卡子树（否则「提交中」也会点到卡片里的禁用选项）。"""
    js = RuiyunUIDriver._AUTO_CONFIRM_JS_TEMPLATE % ('[]', '[]')
    assert "c.includes('agent-question')" in js, "通用路径应排除 agent-question 子树"
    print("PASS 12: 通用路径排除选项卡子树（不再误点卡片内禁用选项）")


def test_generic_path_still_works():
    """非选项卡的授权卡仍走通用两层检测（关键词 / 结构兜底）不被改动。"""
    generic = {"text": "本次允许", "cls": "px-4", "mode": "keyword"}
    drv = make_driver([NOT_FOUND], generic=generic)
    ev = drv._maybe_auto_confirm()
    assert ev and ev["mode"] == "keyword", ev
    assert drv.confirm_events[-1] == ev
    # skip_qcard=True 时不再走卡片路径（同轮去重）
    drv = make_driver([card()], generic=generic)
    ev = drv._maybe_auto_confirm(skip_qcard=True)
    assert ev and ev["mode"] == "keyword", ev
    assert drv.cdp.clicks == [], drv.cdp.clicks
    print("PASS 11: 授权卡通用路径保持不变 / skip_qcard 同轮去重生效")


def test_event_sess_attribution():
    """点击事件带会话归属：显式 owner（巡检）> 当前视图 > 当前在途会话。

    归属必须在点击那一刻记录：并发（max_inflight>1）时事后按时间窗推断必然串台，
    用例卡时间线也就无法把「这次自动确认」挂到正确的用例上。
    """
    # ① 选项卡路径：显式 owner 优先于当前在途会话
    drv = make_driver([card()])
    drv.set_current_sess(str(sess("sess_other")))
    ev = drv._qcard_handle(owner_sess=sess("a"))
    assert ev and ev["sess"] == str(sess("a")), ev
    # ② 巡检路径：视图已切到目标会话，点击自动归属到该会话
    drv = make_driver([NOT_FOUND, card()])
    drv.set_current_sess(str(sess("sess_other")))
    drv.cycle_and_confirm([(sess("a"), FakeCaseResult("你好"))], {sess("a")})
    assert drv.confirm_events, drv.confirm_events
    assert drv.confirm_events[-1]["sess"] == str(sess("a")), drv.confirm_events[-1]
    # ③ 主视图扫描（无显式 owner）→ 当前在途会话兜底
    drv = make_driver([NOT_FOUND], generic={"text": "本次允许", "cls": "px-4", "mode": "keyword"})
    drv.set_current_sess(str(sess("sess_cur")))
    ev = drv._maybe_auto_confirm()
    assert ev and ev["sess"] == str(sess("sess_cur")), ev
    # ④ 转人工：记下当时所在会话，供上游把 P0 挂到正确的用例
    drv = make_driver([card()])
    drv.set_current_sess(str(sess("sess_cur")))
    for _ in range(20):
        drv._qcard_handle()
        if drv.confirm_human_needed:
            break
    assert drv.confirm_human_sess == str(sess("sess_cur")), drv.confirm_human_sess
    print("PASS 13: 点击事件带会话归属（显式 owner > 当前视图 > 当前在途会话），"
          "转人工记录所在会话")


if __name__ == "__main__":
    t0 = time.time()
    test_find_item()
    test_fact_driven_progress()
    test_disabled_and_submitting_never_clicked()
    test_no_progress_stops_early()
    test_fill_free_text_question()
    test_card_reset_on_disappear()
    test_view_lock_prevents_switching()
    test_degrade_on_broken_sidebar()
    test_skip_settled()
    test_disabled_flag()
    test_generic_path_still_works()
    test_generic_excludes_qcard_subtree()
    test_event_sess_attribution()
    print(f"\n全部 13 项注入式测试通过 ✓（{time.time() - t0:.2f}s）")
