---
name: 创建 CODEBUDDY.md
overview: 在仓库根目录新建 CODEBUDDY.md：先给出常用的运行/测试/诊断命令（每条说明 ≤100 字），再用不超过 1600 字描述跨文件才能理解的「大图景」架构。
todos:
  - id: collect-facts
    content: 核对无 AGENTS.md/CODEBUDDY.md，汇总 README、config.yaml 与脚本中的命令及架构事实
    status: completed
  - id: draft-commands
    content: 编写 CODEBUDDY.md 前缀与常用命令区，逐条描述不超过 100 字并标注产出物
    status: completed
    dependencies:
      - collect-facts
  - id: draft-architecture
    content: 补写架构总览区（流水线/驱动/断言/报告/服务），总计不超过 1600 字
    status: completed
    dependencies:
      - draft-commands
  - id: review-doc
    content: 校验字数上限、无虚构内容与重复表述，确认文件落于仓库根目录
    status: completed
    dependencies:
      - draft-architecture
---

## 用户需求

为当前仓库创建一份 `CODEBUDDY.md`，供后续在 CodeBuddy（Coding IDE）中操作本仓库的 Agent 使用。

## 前置检查结果

经核对，仓库根目录**不存在** `AGENTS.md`、`CODEBUDDY.md`、`CLAUDE.md`、`.cursorrules`、`.cursor/rules/`、`.github/copilot-instructions.md`，唯一文档是 `README.md`（中文，约 23KB）。因此按规则**新建 `CODEBUDDY.md`**（而非改动 AGENTS.md），并需提炼 README 中的重要信息。

## 核心内容要求

- 文件必须以指定前缀开头：`# CODEBUDDY.md This file provides guidance to CodeBuddy when working with code in this repository.`
- **常用命令**：覆盖运行/测试/调试/运维类命令（完整流水线、指定用例、离线回放、复现率验证、可视化平台、UI 诊断、环境验证、注入式测试、应用启停），**每条命令描述不超过 100 字**。
- **高层架构**：说明需要跨多个文件才能理解的“大图景”（四段式流水线、CDP 驱动与应用生命周期、日志解析与断言、指标与报告、本地服务与前端、配置集中点、关键环境坑），**架构描述不超过 1600 字**。
- 遵守约束：不重复、不逐文件罗列易发现的结构、不写通用开发建议（如“写单测”“做好错误提示”）、不虚构 README 源码中不存在的信息。

## 视觉/交付效果

产出为一份结构化中文 Markdown 文档，位于仓库根目录，可直接被后续 Agent 读取并据此上手；命令可复制执行，架构描述可独立理解。

## 技术栈

本任务为**仓库文档编写**，不引入新代码依赖。写作依据来自仓库现有实现：

- 运行环境：Python 3（README 统一使用 `/Users/amano/.workbuddy/binaries/python/envs/default/bin/python`，脚本内记作 `PY`）。
- 运行时依赖：`pyyaml`、`jinja2`、`websocket-client`；其余为标准库（`http.server`、`urllib`、`subprocess`）。仓库**无** `requirements.txt`/`pyproject.toml`，**无** lint 配置，**无** pytest。
- 被文档化的主体：`run_pipeline.py`（编排）、`core/*`（解析/断言/指标/复现/轨迹）、`drivers/*`（CDP 与 UI 驱动）、`report/*`（HTML 报告）、`server.py` + `web/index.html`（可视化平台）、`config.yaml`/`testcases.yaml`（配置与用例）、`scripts/*`（诊断与注入式测试）、`launch_app_dev.sh`/`start_platform.command`（运维脚本）。

## 实现方案

新建单一文档 `CODEBUDDY.md`，按“命令 → 架构 → 关键约束”三段组织，内容全部**可溯源**：

1. **固定前缀**：逐字使用用户指定的一行前缀作为首行。
2. **常用命令区**（逐条 ≤100 字）：从 README 与脚本文档串中提取，按“测试/运行、复现验证、可视化平台、诊断与验证、应用生命周期”分组，并给出产出物位置。
3. **架构总览区**（总计 ≤1600 字）：以跨文件概念为核心——

- **四段式流水线**：Stage 1 UI 驱动 → Stage 2 链路断言 → Stage 2.5 复现率验证 → Stage 3 报告（含 `--run-id` 轮次归档）。
- **驱动与生命周期**：`drivers/cdp.py` 极简 CDP（本机请求排除代理）；`drivers/ui_driver.py` DOM 级定位、探测/复用/按需启动并注入 `env_profile`、等待主 UI 就绪（排除 `data:` 外壳页）、`reset_to_new_task` 保证一例一会话、判稳（size+mtime + `_looks_complete`）、`auto_confirm` 两层检测与护栏、多会话视图巡检与两段式选项卡状态机。
- **执行模型**：“发送串行、生成并行”（`max_inflight`）；`case_timeout_s` 是等待上限（`waited_limit` 中性，非 ui_error）。
- **日志与断言**：`session/sess_*/session.messages.json` 结构；工具结果三种形态兼容；`unwrap_result`/`extract_body`；`core/assertor.py` 规则与 `config.yaml` 阈值集中；`OUTPUT_TRUNCATED` 多信号推断且只判最终答案。
- **指标/报告双通道**：`core/metrics.build_metrics`（HTML 报告）与 `core/trajectory.build_round_detail`（可视化平台逐步骤全文）。
- **本地服务与前端**：`server.py` 纯标准库 REST + 子进程运行流水线；`web/index.html` 单文件前端消费其 API。
- **配置集中点**：`config.yaml` 为唯一可调入口；`testcases.yaml` 仅作“导入预设”模板。

4. **关键约束/坑区**：README 明确列出的踩坑（清除 `ELECTRON_RUN_AS_NODE`、`--disable-gpu --no-sandbox`、绕过系统代理、整轮结束才落盘、每条用例前回首页、环境变量启动时注入故运行中只读）——这些跨文件才能理解，属于高价值内容。

## 执行要点

- 只用已核实事实（文件路径、参数名、行为），**不编造**未在仓库出现的命令或参数。
- 命令描述控制字数，避免与 README 大段重复；架构区以“为什么/如何协作”为主，不逐文件罗列。
- 不添加“常见开发任务/技巧/支持文档”等仓库未声明的章节。

## 目录结构（变更范围）

```
ruiyun-ui-test-platform/
└── CODEBUDDY.md   # [NEW] 仓库根目录新建的 Agent 操作指南。以用户指定前缀开头，
                   # 含：(1) 常用命令区——流水线/复现/可视化平台/诊断/应用启停，逐条≤100字，
                   #     并标注产出物路径；(2) 架构总览区——四段式流水线、CDP 驱动与生命周期、
                   #     发送串行生成并行、日志解析与断言规则、指标/报告双通道、本地服务与前端、
                   #     配置集中点，总计≤1600字；(3) 关键环境坑与硬约束。
                   # 内容全部溯源 README.md、config.yaml、运行脚本与 core/drivers 源码，禁止虚构。
```