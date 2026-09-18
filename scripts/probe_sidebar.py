#!/usr/bin/env python3
"""只读探查：应用侧边栏会话列表的 DOM 结构（为视图巡检采集 selector 情报）。

不点击、不输入，仅 eval_js 读取。输出写入 artifacts/sidebar_probe.json。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from core.settings import config_path  # noqa: E402
from drivers.ui_driver import RuiyunUIDriver  # noqa: E402

JS = r"""
(() => {
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0; };

  // 1) 找侧边栏容器：class 含 sidebar/aside/nav 的最外层
  const containers = [...document.querySelectorAll('aside,[class*="sidebar" i],[class*="side-bar" i],[class*="sider" i]')]
    .filter(vis).map(el => ({
      tag: el.tagName, cls: (el.className||'').toString().slice(0,100),
      rect: (()=>{const r=el.getBoundingClientRect();
        return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)];})()
    }));

  // 2) 列表项候选：sidebar 容器内的可点击行（li / [role=item|button] / 带点击类的 div）
  const sideRoot = document.querySelector('aside,[class*="sidebar" i]');
  let items = [];
  if (sideRoot) {
    const cands = [...sideRoot.querySelectorAll('li,[role="listitem"],[role="button"],div[class*="item" i],div[class*="session" i],div[class*="conversation" i],a')]
      .filter(vis);
    const seen = new Set();
    items = cands.filter(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 24 || r.height > 120) return false;  // 行高过滤
      if (seen.has(el.parentElement)) return false;                        // 只留最深一层
      return true;
    }).slice(0, 40).map(el => {
      const r = el.getBoundingClientRect();
      return {
        tag: el.tagName,
        cls: (el.className||'').toString().slice(0,120),
        text: (el.innerText||'').trim().replace(/\s+/g,' ').slice(0,60),
        dataAttrs: [...el.attributes].filter(a=>a.name.startsWith('data-'))
                    .map(a=>a.name+'='+a.value.slice(0,30)),
        rect: [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],
        hasSpinner: !!el.querySelector('[class*="spin" i],[class*="loading" i],[class*="stop" i],svg[class*="loading" i]'),
        childCls: [...el.children].map(c=>(c.className||'').toString().slice(0,50)).filter(Boolean).slice(0,6),
      };
    });
  }

  // 3) 全局找"生成中"迹象：旋转/停止/加载类元素的位置与 class
  const genHints = [...document.querySelectorAll('[class*="spin" i],[class*="loading" i],[class*="stop" i],[class*="generating" i],[class*="streaming" i]')]
    .filter(vis).slice(0, 15).map(el => {
      const r = el.getBoundingClientRect();
      return { cls: (el.className||'').toString().slice(0,100), tag: el.tagName,
               rect: [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],
               inSidebar: !!el.closest('aside,[class*="sidebar" i]') };
    });

  // 4) 主视图当前状态：第一条用户消息前 40 字（判断当前渲染的是哪个会话）
  const userMsgs = [...document.querySelectorAll('[class*="user" i][class*="message" i],[class*="message" i][class*="user" i],div[class*="question" i]')]
    .filter(vis).slice(0, 3).map(el => (el.innerText||'').trim().replace(/\s+/g,' ').slice(0,40));

  return { url: location.href.slice(0, 80), containers, items, genHints, userMsgs };
})()
"""

def main() -> int:
    cfg = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    drv = RuiyunUIDriver(cfg)
    if not drv.attach():
        print("[x] 无法连接渲染进程（应用未运行或未登录）")
        return 1
    time.sleep(3)
    try:
        info = drv.cdp.eval_js(JS, timeout=15)  # pyright: ignore[reportOptionalMemberAccess]
    except Exception as e:
        print(f"[x] 读取失败: {e}")
        return 1
    out = Path(__file__).resolve().parent.parent / "artifacts" / "sidebar_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"URL: {info.get('url')}")
    print(f"\n== 侧边栏容器 ({len(info.get('containers', []))}) ==")
    for c in info.get("containers", [])[:6]:
        print(f"  <{c['tag']}> {c['cls'][:70]} rect={c['rect']}")
    print(f"\n== 列表项 ({len(info.get('items', []))}) ==")
    for it in info.get("items", [])[:25]:
        flag = " [spinner]" if it["hasSpinner"] else ""
        print(f"  <{it['tag']}> {it['cls'][:60]!r} text={it['text'][:40]!r}{flag}")
        if it["dataAttrs"]: print(f"      attrs: {it['dataAttrs']}")
        if it["childCls"]:  print(f"      children: {it['childCls']}")
    print(f"\n== 生成中迹象 ({len(info.get('genHints', []))}) ==")
    for g in info.get("genHints", [])[:10]:
        print(f"  <{g['tag']}> {g['cls'][:70]!r} rect={g['rect']} inSidebar={g['inSidebar']}")
    print(f"\n== 主视图用户消息样例 == {info.get('userMsgs')}")
    print(f"\n完整 JSON → {out}")
    drv.detach()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
