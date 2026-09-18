#!/usr/bin/env python3
"""探查输入区（composer）的附件能力：文件输入框 / 上传按钮 / 拖拽落区。

为什么需要它：附件投递方式完全取决于应用前端的实现 ——
  * 网页式 `<input type="file">` → 可用 CDP `DOM.setFileInputFiles` 直接投递（最可靠）；
  * Electron 原生选择框 → DOM 塞不进去，只能改走拖拽或「放进工作区」的绕行方案。
写错方向的后果是「附件根本没送进去，但用例照样跑完」——即假通过。
本脚本只读探查，不改动应用任何状态。

用法：
    python scripts/probe_composer.py            # 连接已开启调试端口的实例
    python scripts/probe_composer.py --launch   # 未运行则自动以调试模式启动
    python scripts/probe_composer.py --new-task # 先回到「新建任务」首页再探查（推荐）

输出：artifacts/composer_probe.json + 终端摘要
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from core.settings import config_path  # noqa: E402

from drivers.ui_driver import RuiyunUIDriver  # noqa: E402

PROBE_JS = r"""
(() => {
  const info = (el) => {
    const attrs = {};
    for (const a of (el.attributes || [])) attrs[a.name] = a.value;
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: (el.className || '').toString().slice(0, 140),
      type: el.getAttribute('type'),
      accept: el.getAttribute('accept'),
      multiple: el.hasAttribute('multiple'),
      aria: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      dataTest: el.getAttribute('data-testid') || el.getAttribute('data-test'),
      text: (el.innerText || el.value || '').trim().slice(0, 40),
      visible: r.width > 0 && r.height > 0,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    };
  };
  // 1) 文件输入框（可能被隐藏，隐藏也能用 DOM.setFileInputFiles 塞文件）
  const fileInputs = [...document.querySelectorAll('input[type="file"]')].map(info);

  // 2) 文案/类名疑似「附件 / 上传 / 文件」的元素
  const RE = /(附件|上传|文件|图片|图像|照片|文档|粘贴|拖拽|upload|attach|attachment|clip|paperclip|dropzone|drop-zone|file)/i;
  const hit = (el) => {
    const c = (el.className || '').toString();
    return RE.test(c) || RE.test(el.getAttribute('aria-label') || '')
        || RE.test(el.getAttribute('title') || '')
        || RE.test((el.innerText || '').trim());
  };
  const candidates = [...document.querySelectorAll('button,[role="button"],label,div,span,svg,i,a')]
    .filter(hit).slice(0, 60).map(info);

  // 3) 输入框的祖先链（类名能反映 composer 结构）
  const inp = document.querySelector('textarea,div[contenteditable="true"],[role="textbox"]')
           || document.querySelector('input[type="text"]');
  const chain = [];
  let n = inp;
  for (let i = 0; n && i < 6; i++) {
    chain.push({ tag: n.tagName.toLowerCase(), cls: (n.className || '').toString().slice(0, 140),
                 children: n.children.length });
    n = n.parentElement;
  }
  const top = chain.length ? document.querySelector('textarea,div[contenteditable="true"],[role="textbox"]') : null;
  let box = top;
  for (let i = 0; box && i < 4 && box.parentElement; i++) box = box.parentElement;
  return {
    url: location.href,
    hasFileInput: fileInputs.length > 0,
    fileInputs: fileInputs,
    candidates: candidates,
    inputChain: chain,
    composerHTML: box ? box.outerHTML.slice(0, 5000) : '',
  };
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="探查 composer 的附件能力（只读）")
    ap.add_argument("--launch", action="store_true", help="应用未运行时自动以调试模式启动")
    ap.add_argument("--new-task", action="store_true", help="先回到「新建任务」首页再探查")
    ap.add_argument("--settle", type=float, default=3.0, help="连接后等待渲染的秒数")
    args = ap.parse_args()

    cfg = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    drv = RuiyunUIDriver(cfg)
    if args.launch:
        ok, how = drv.ensure_ready()
        if not ok:
            print("[x] 应用启动或调试端口就绪失败")
            return 1
        print(f"[i] {'复用已运行的应用' if how == 'reused' else '已启动应用'}")
    elif not drv.attach():
        print("[x] 无法连接渲染进程（应用可能未开调试端口）")
        return 1

    import time
    time.sleep(args.settle)
    if args.new_task and not drv.reset_to_new_task():
        print("[!] 未能回到「新建任务」首页，仍继续探查当前视图")

    info = drv.cdp.eval_js(PROBE_JS, timeout=20) or {}
    print(f"URL   : {info.get('url')}")
    print(f"文件输入框数量: {len(info.get('fileInputs') or [])}"
          f"（hasFileInput={info.get('hasFileInput')}）")
    for el in (info.get("fileInputs") or []):
        print(f"  <{el['tag']}> type={el.get('type')} accept={el.get('accept')!r} "
              f"multiple={el.get('multiple')} visible={el['visible']} rect={el['rect']}")
        print(f"      id={el.get('id')!r} cls={(el.get('cls') or '')[:90]}")

    print("\n--- 疑似「附件/上传」元素（取前 20）---")
    for el in (info.get("candidates") or [])[:20]:
        print(f"  <{el['tag']}> visible={el['visible']} text={el.get('text')!r} "
              f"aria={el.get('aria')!r} title={el.get('title')!r}")
        print(f"      cls={(el.get('cls') or '')[:110]} rect={el['rect']}")

    print("\n--- 输入框祖先链 ---")
    for c in (info.get("inputChain") or []):
        print(f"  <{c['tag']}> children={c['children']} cls={(c['cls'] or '')[:110]}")

    out = Path(__file__).resolve().parent.parent / "artifacts"
    out.mkdir(parents=True, exist_ok=True)
    (out / "composer_probe.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n完整 JSON 已写入 artifacts/composer_probe.json")
    drv.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
