# 睿云智能工作台 · UI 测试平台 (Ruiyun UI Test Platform)

这是一个针对「睿云智能工作台 (Ruiyun Smart Workspace)」的自动化 UI 测试与日志验证平台。它可以通过真实的 UI 交互注入测试用例，捕获应用侧的会话日志，进行断言分析，并最终生成测试报告。

## 平台特性

- **UI 自动化驱动 (UI Driver)**：基于大模型应用的界面特点，提供对会话流、大模型选项卡、附件拖拽等核心场景的无痕自动化控制。
- **全链路日志分析 (Log Parsing)**：不仅通过 UI 验证结果，还能通过分析底层会话日志（如思考过程、工具调用）来确保系统的内部逻辑正确。
- **流水线编排 (Pipeline)**：三段式全流程测试（UI 操作 -> 日志断言 -> 报告生成），提供开箱即用的测试能力。
- **Web 可视化控制台 (Web UI)**：内置可视化 Web 端控制台，可直观地查看各轮次的测试报告、问题发现和指标详情。
- **本地 / CI 环境兼容**：支持在开发环境中实时查看效果，也可在流水线中进行无头静默测试。

## 目录结构

- `core/`：核心逻辑模块（包含日志解析 `log_parser.py`、验证器 `assertor.py`、大模型评估 `evaluator.py` 及相关配置和指标统计等）。
- `drivers/`：UI 驱动控制模块（如 `ui_driver.py`），负责与底层应用进行实际交互。
- `report/`：测试报告生成模块（`builder.py`），负责将测试结果渲染为可视化视图。
- `web/`：测试平台的 Web 控制台前端页面（`index.html`）。
- `scripts/`：测试探针与辅助开发脚本（例如用于验证 DOM 结构、特定组件交互的诊断脚本）。
- `tests/`：用于平台自身的单元测试存放目录。

## 核心入口

- **`run_pipeline.py`**：完整测试流水线主入口。按顺序执行 UI 操作注入、日志验证、并构建测试报告。
  - 用法示例：`python run_pipeline.py` 或 `python run_pipeline.py --cases 1`
- **`run_repro.py`**：重现特定测试步骤或用例的入口，便于在出现 Bug 时快速复现。
- **`server.py`**：平台内建 Web 服务端，用于运行和托管 Web 可视化控制台。
- **`discover_ui.py`**：DOM 提取探针工具，用于导出并分析应用当前的 UI 结构。
- **`start_platform.command` / `launch_app_dev.sh`**：快速启动工作台/测试平台服务的快捷脚本。

## 快速开始

1. **环境准备**
   请确保您已经安装了对应的 Python 依赖，建议在虚拟环境中运行：
   ```bash
   pip install -r requirements.txt
   ```

2. **运行全链路测试**
   ```bash
   python run_pipeline.py
   ```
   首次运行时，用户工作区（macOS `~/.ruiyun-autotest/`、Windows `%USERPROFILE%\.ruiyun-autotest\`）会自动按仓库根的 `config.template.yaml` 初始化出 `config.yaml`：里面有环境档案、断言阈值等全部可调项。被测应用路径无需手动填写 —— 留空即用本机内置默认（macOS `/Applications/睿云智能工作台.app/Contents/MacOS/睿云智能工作台`，Windows `C:\Program Files\srtclaw\睿云智能工作台.exe`），也可在控制台「⚙ 设置 → 应用与工作区」中改。

3. **启动测试平台控制台**
   您可以直接运行 `start_platform.command`（Mac）/ `start_platform.bat`（Windows），或者启动服务端以在浏览器中查看可视化测试结果：
   ```bash
   python server.py
   ```
