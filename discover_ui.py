#!/usr/bin/env python3
"""UI 诊断工具：连接已开启调试端口的睿云智能工作台，导出 DOM 中可交互元素。

用法：
    python discover_ui.py              # 连接现有实例（已运行则直接复用）
    python discover_ui.py --launch     # 未运行则自动以调试模式启动
    python discover_ui.py --close-app  # 探查结束后显式关闭应用（默认不关）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from core.settings import config_path  # noqa: E402
from drivers.ui_driver import RuiyunUIDriver  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true",
                    help="应用未运行时自动以调试模式启动（已运行则复用）")
    ap.add_argument("--settle", type=float, default=4.0, help="连接后等待渲染的秒数")
    ap.add_argument("--close-app", action="store_true",
                    help="探查结束后关闭应用（默认保持运行，遵循平台常驻策略）")
    args = ap.parse_args()

    cfg = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    # 应用路径 / 日志路径三层合并：.app_settings.json > config.yaml > 内置默认
    from core.settings import effective_config
    cfg = effective_config(cfg)
    drv = RuiyunUIDriver(cfg)

    if args.launch:
        ok, how = drv.ensure_ready()
        if not ok:
            print("[x] 应用启动或调试端口就绪失败")
            return 1
        print(f"[i] {'复用已运行的应用' if how == 'reused' else '已启动应用'}")
    else:
        if not drv.attach():
            print("[x] 无法连接渲染进程")
            return 1

    import time
    time.sleep(args.settle)
    info = drv.discover()

    print(f"URL   : {info.get('url')}")
    print(f"TITLE : {info.get('title')}")
    print(f"BODY  : {len(info.get('bodyText') or '')} 字符")

    print("\n--- 输入类元素 ---")
    for el in info.get("inputs", [])[:12]:
        print(f"  <{el['tag']}> visible={el['visible']} rect={el['rect']}")
        print(f"      placeholder={el.get('placeholder')!r} aria={el.get('ariaLabel')!r}")
        print(f"      class={(el.get('cls') or '')[:90]}")

    print("\n--- 按钮类元素 ---")
    for el in info.get("buttons", [])[:25]:
        if not el["visible"]:
            continue
        print(f"  <{el['tag']}> text={el.get('text')!r} aria={el.get('ariaLabel')!r} "
              f"rect={el['rect']} class={(el.get('cls') or '')[:70]}")

    print("\n完整 JSON 已写入 artifacts/ui_dom.json")
    out = Path(__file__).parent / "artifacts"
    if not out.is_dir():
        out.mkdir(parents=True)
    (out / "ui_dom.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.close_app:
        drv.kill_app()
        print("[i] 已按 --close-app 关闭应用")
    else:
        drv.detach()
        print("[i] 已断开连接（应用保持运行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
