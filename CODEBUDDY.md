# CODEBUDDY.md This file provides guidance to CodeBuddy when working with code in this repository.

## 常用命令

约定：下文的 `$PY` 指项目根目录的 `.venv/bin/python`（与 `start_platform.command` 一致；README 中同根目录的 macOS 启动脚本走的是同目录版）。运行时依赖见 `requirements.txt`（仅 `PyYAML`、`Jinja2`、`websocket-client`；其中 websocket-client 按 Python 版本分档：3.9 用 1.9.0，3.10+ 用 1.9.2），其余为标准库。无 lint 配置、无 pytest —— 全部自测脚本是纯 `assert` + `__main__`，无单条筛选机制。

### 运行测试（UI 自动化 + 断言 + 报告）

- `$PY run_pipeline.py` — 完整三段式流水线：驱动 UI 跑 `testcases.yaml` 预设用例，解析日志断言后出报告。
- `$PY run_pipeline.py --cases-file my_cases.yaml` — 改用手写用例文件（YAML/JSON，界面输入走这条路径）；只填 `prompt` 也能跑。
- `$PY run_pipeline.py --cases 3` — 只执行前 3 条用例，用于快速验证。
- `$PY run_pipeline.py --repro-times 1 --repro-limit 2` — 跑完后对 P0 优先的 2 个 bug 签名各复现 1 次，量化复现率。
- `$PY run_pipeline.py --max-inflight 3` — 覆盖并发数（同时在途会话上限），不传则用 `config.yaml` 的值。
- `$PY run_pipeline.py --run-id run_xxx` — 归档本轮全部数据到用户工作区 `~/.ruiyun-autotest/rounds/<run_id>/`（可视化平台即由服务端传入此参数）。

### 复现率验证

- `$PY run_repro.py --list` — 只列出当前日志里所有 bug 的复现配方，不执行，零成本。
- `$PY run_repro.py --rule TOOL_CALL_FAILED --tool read_memory --times 3` — 对指定签名驱动 UI 重复执行 3 次，统计复现率。
- `$PY run_repro.py --all --times 2 --update-report` — 验证全部签名并重渲染 HTML 报告；正式结论建议 `--times 5`。
- `$PY run_repro.py --render-only` — 不驱动 UI，用上次 `artifacts/repro_results.json` 重渲染报告。

### 可视化平台

- `$PY server.py --port 8765` — 启动本地仪表盘（纯标准库），浏览器打开 `http://127.0.0.1:8765`。
- `$PY server.py --port 8765 --allow-lan` — **临时**放行局域网访问（绑定 0.0.0.0，放行私网 IP 的 Host/Origin，公网来源仍 403）。服务自身有同源防护（`_same_origin_ok`：仅允许 127.0.0.1/localhost，挡 CSRF 与 DNS 重绑定），默认不开放行；用完去掉参数重启。
- `./start_platform.command` — 双击即启动服务并自动开浏览器；已在运行时会直接复用。

### 诊断与验证

- `$PY discover_ui.py --launch` — 连接（或按需启动）应用并导出 DOM 中可交互元素到 `artifacts/ui_dom.json`，用于排查选择器。加 `--close-app` 可在探查后关闭应用。
- `$PY scripts/verify_env.py` — 通过后端进程实际外联 IP 判定当前实例是 dev 还是生产环境。
- `$PY scripts/probe_sidebar.py` — 只读探查侧边栏会话列表的 DOM 结构，输出 `artifacts/sidebar_probe.json`。
- `$PY scripts/test_cycle_confirm.py` — 自动确认逻辑自测：13 项注入式断言，用假 CDP 模拟 DOM，不驱动真实应用。覆盖选项卡事实驱动推进（选项→确认/提交）、提交中禁用不点击、点击被拒不算成功、零推进 6 次即转人工、自由输入题自动填写、卡片消失复位、**选项卡独占视图**、巡检降级与通用路径排除卡片子树、**点击事件带会话归属**（显式 owner > 当前视图 > 当前在途会话）与转人工会话记录。
- `$PY scripts/test_timeline_merge.py` — 自动确认事件并入用例时间线的自测（6 项注入式）：按会话归属互不串台、按消息段落入段尾且段内时间升序、早于首段/晚于末段/无时间锚的确定回落、无轨迹仍输出条目、无 `message_spans` 的历史轨迹退化行为。
- `$PY scripts/test_evaluator.py` — 质量评估自测（20 组，不联网、不驱动 UI）：指标口径、客观维度、判分失败一律不给分、前置探活拒绝启动、密钥不泄漏且**不触碰真实密钥文件**、安全红线不误判「拒绝/警告」、**全维度由模型判定且只送客观事实**、**红线硬覆盖生效/可关闭**；样例轮次为**动态选取**（避免硬编码 run_id 被删后整组静默跳过），端到端用例以构造轮次为主（会话日志在轮次目录之外会被应用实时改写，复制轮次拿不到稳定快照）。
- `$PY scripts/test_llm_probe.py` — 模型探活自测（11 项）：错误分类、端点回退、Key 不泄露。
- `$PY scripts/diagnose_llm.py` — 评估判分失败时的排障入口：一次打印脱敏配置 + 本机直连出口 IP（多源比对）+ 供应商返回原文，并给出「该把哪个 IP 加入白名单 / 该填什么模型名」的结论；`--ip-only` 只看出口 IP（不调用模型、不产生费用）。
- `$PY scripts/fake_openai_server.py --port 8899` — 本地假供应商（OpenAI 兼容，零依赖），用于离线跑通「评估 → 落盘 → 前端」全链路；加 `--fail-403` 可演练「来源 IP 未在白名单」的失败路径。
- `$PY scripts/probe_composer.py --new-task` — 只读探查输入区（composer）的附件能力：文件输入框 / 上传按钮 / 拖拽落区，输出 `artifacts/composer_probe.json`。**应用改版后附件入口若有变化，先跑它**再改驱动。
- `$PY scripts/probe_qcard.py`（`--walk` 逐题走查 / `--drive` 用驱动自身推进 / `--switch-test` 验证切视图是否重置卡片）— 选项卡专项探针：投递本机附件触发 `agent-question-composer`，把题号/选项/按钮三态事实逐帧落盘到 `artifacts/qcard_probe*.json`。**应用改版后卡片结构若有变化，先跑它**再改驱动。
- `$PY scripts/test_attach.py` — 附件投递自测（4 项注入式，不驱动应用）：入参护栏、**引用计数未增加必须判失败**（防假通过）、部分投递判失败。

### 应用生命周期与实验

- `./launch_app_dev.sh --detach`（或 `--stop` / `--dry-run`）— 以 dev 环境变量启动/结束应用；也可用 `DEBUG_PORT`、`LOG_FILE` 覆盖默认值。平台本身默认永不关闭应用。
- `$PY experiment_pipeline.py` — 复跑「生成中点新建任务」的流水线可行性实验，`max_inflight>1` 的依据。
- `$PY experiment_confirm.py` — 复现并捕获选项卡（`agent-question-composer`）两段式自动确认全流程。

### 质量评估与模型接入

- `$PY server.py --port 8765` 后，在网页顶栏「⚙ 模型设置」填写供应商地址 / API Key / 模型名，先「测试连接」再保存（写入 `.llm_secrets.json`，0600、已 gitignore，**密钥绝不进版本库**）。
- 评估入口在轮次详情的「质量评估」页签：`POST /api/evaluate` 启动后台任务，`GET /api/eval/status` 轮询进度，结果落 `~/.ruiyun-autotest/rounds/<run_id>/evaluation.json`。
- 模型名为**必填**：留空会让判分请求带上占位模型名，换回与真实原因无关的报错。评估启动前会先用已存配置真实探活一次，不通过则拒绝开始（不烧额度、不覆盖既有 `evaluation.json`）。
- 用例的「场景」与「是否要求交付文件」以用例自身声明的 `labels` 为准；界面手输用例没有标签时按问题原文推断，界面标「推断」（判断不出则保持未分类，两组都不评）。导入预设时会连标签一起导入——**绝不按同名 id 借预设标签**，因为手输用例 id 是自动序号 `CASE-001…`，与预设 id 必然重名。
- 评估结果以**三列表格**呈现，表头固定为：结果质量 / 教学专业质量 · 安全与可靠 · 系统与 Agent 能力（前两者合并成一列，因为同一用例只属于其中一类）。**每个用例一张表**（行=指标，单元格=该用例该项分值，表尾一行=「维度均分」——只聚合**本用例**各列已评分项，不跨用例），表上方给出该用例的场景（推断会标「推断」）与产物要求。列定义与指标顺序均由后端下发（`core/eval_rubric.EVAL_COLUMNS` → `summary.columns` / `dim_order`），前端不再自拼口径。本轮测试综合分模块与**跨用例均分**均已删除（后端仍落盘 `column_means` 供 API 消费者），每张逐用例表表尾只给本用例各维度均分；轮次详情的「完整报告 ↗」页签已删除（`report.html` 仍由流水线归档产出）。界面**悬停任一指标**会显示该指标的档位锚点原文、分值来源（模型判定 / 硬规则）与未评分原因；这份说明同样由后端随产物下发（`core/eval_rubric.rubric_meta()` → `evaluation.json.rubric`），因此历史结果始终用它**当时的**锚点自我解释。每条用例卡底部还有「**模型收到的客观事实（判分依据）**」折叠块（`cases[].objective_facts`），可离线核对「模型当时看到了什么（含本地参考值）」。
- `$PY scripts/diagnose_llm.py` — 判分失败时先跑它，再决定改配置还是改网络。

### 产出物

| 文件 | 说明 |
|---|---|
| `report/ruiyun_hardbug_report.html` | 可视化测试报告（自包含，双击可看） |
| `artifacts/metrics.json` | 全部量化指标，供 CI 消费 |
| `artifacts/case_results.json` | 逐用例执行与断言明细 |
| `artifacts/repro_results.json` | 复现配方与复现率验证结果 |
| `~/.ruiyun-autotest/rounds/<run_id>/` | 单轮归档：`cases.yaml`、`round_detail.json`、`round_summary.json`、`report.html`、`console.log` |
| `~/.ruiyun-autotest/rounds/<run_id>/evaluation.json` | 质量评估结果：逐用例 20 项分值（含 `nature`=维度性质 / `basis`=分值来源 / `llm_used` / `hint_score`=本地参考值 / `na_reason` / `evidence`）、`cases[].objective_facts`（发给模型的客观事实包）、`cases[].artifacts[].abs_path`（产物绝对路径）与顶层 `workspace_root`（产物解析根，供界面「产物目录」用）与 `summary`（`score_source`、`scored_cases`、`failed_cases`、`llm_coverage`、`judge_errors`、`redline_overrides`）；顶层 `scoring_mode` 标识评分口径 |
| `artifacts/uploads/` | 用例附件的本机落盘位置（已 gitignore）；运行时由驱动拖拽投递给被测应用 |
| `.llm_secrets.json` | 模型凭证（0600，已 gitignore）；**跑自测不会触碰它** |

## 架构总览

**目标与口径**：对 Electron 应用「睿云智能工作台」做全自动 UI 测试——驱动界面 → 断言调用链路 → 生成可视化报告。每轮只统计本轮对话新增数据，不追溯历史；只判应用硬 bug，不评判内容质量。

**四段式流水线**（`run_pipeline.py`）：Stage 1 UI 自动化（`drivers/ui_driver.py`）→ Stage 2 链路断言（`core/log_parser.py` → `core/assertor.py`）→ Stage 2.5 复现率验证，可选（`core/repro.py`）→ Stage 3 报告（`core/metrics.py`、`core/trajectory.py` + `report/builder.py`）。传 `--run-id` 时归档到用户工作区 `rounds/<run_id>/`。

**驱动与生命周期**：`drivers/cdp.py` 是仅依赖标准库 + websocket-client 的极简 CDP 客户端，对 `127.0.0.1` 显式排除 HTTP_PROXY。`drivers/ui_driver.py` 全程 DOM 级定位，负责探测/复用/按需启动应用并注入 `env_profile`（默认 dev）、等待主 UI 就绪（排除 `data:` 外壳页）、输入发送、`reset_to_new_task()` 保证「一例一会话」、按 size+mtime 判稳，以及 `auto_confirm` 自动点击计划确认/工具授权卡片（关键词优先 + 选项组结构兜底，拒绝词永不点，含冷却与熔断转人工；**每次点击记录所属会话目录 `sess`**，供用例级归属与时间线混排 —— 并发下这是唯一可靠的归属信息源）。平台永不主动关应用：端口活着即复用，结束只 `detach()`。

**执行模型**：发送串行、生成并行——每条发送并确认新会话目录出现后才发下一条，在途最多 `max_inflight` 条，谁先完成先收。`case_timeout_s` 是等待上限而非判罚：等满即解析已落盘日志出结果，记 `waited_limit`（中性），不写 `ui_error`、不影响通过率。

**日志与断言**：`core/log_parser.py` 把 `session/sess_*/session.messages.json` 规范化为 `ExecutionTrace`；工具结果有纯文本 / JSON 字符串 / Python repr 三种形态，需全部兼容；`unwrap_result` 下钻外层信封，`extract_body` 取内层正文供长度与截断判定。`core/assertor.py` 有 11 条硬 bug 规则 + `CONFIRM_MANUAL_NEEDED`，阈值集中在 `config.yaml` 的 `rules:`；`OUTPUT_TRUNCATED` 因日志无 `finish_reason` 改用多信号交叉推断，且只判最终答案。

**指标与服务**：`core/metrics.build_metrics` 供 HTML 报告（本轮概览 + 逐用例）；`core/trajectory.build_round_detail` 供仪表盘逐步骤时间线（思考与工具结果全文不截断），并把平台自动确认事件按会话归属后**混排进同一时间线**（位置为消息级近似：日志只到消息粒度，见 `_merge_confirm_events`），模板为 `report/template.html`。`server.py` 是纯标准库 HTTP 服务，以子进程运行 `run_pipeline.py` 并回收 stdout，提供轮次、运行控制、环境切换、预设用例、Finder 定位等 REST；`web/index.html` 单文件前端只消费这些接口。**预设用例可在界面增删**：`POST /api/preset-cases`（`add`/`delete`）直接写回 `testcases.yaml`（头部标签口径注释保留、原子替换、id 取最大 `CASE-N` 顺延；`width=4096` 保证未触碰条目字节级不变），前端入口在「导入预设」弹窗（已更名「预设用例管理」）。**非功能性数据集中在用户工作区**（`core/settings.py`：`user_workspace()` / `preset_path()` / `uploads_dir()` / `rounds_dir()` / `config_path()` / `secrets_path()`）：默认 `~/.ruiyun-autotest/`（macOS `~/`、Windows `%USERPROFILE%`；本应用将来打包为开箱即用 app），内含 `testcases.yaml`（预设用例）、`uploads/`（附件库）、`rounds/`（轮次归档）、`config.yaml`（平台配置）、`.llm_secrets.json`（模型密钥）。可经「应用设置」的 `user_workspace` 覆盖；历史位置（仓库根 `testcases.yaml` / `config.yaml` / `.llm_secrets.json`、`artifacts/uploads|rounds/`、旧默认 `<平台根>/workspace/`）**自动跟随迁移**（幂等 move，按优先级取第一个存在的旧位置）。顶栏「⚙ 设置」二级菜单（应用与工作区 / 模型设置 / 打开工作区文件夹）→ `POST /api/workspace/open` 在文件管理器直接打开。消费方（`run_pipeline.load_cases` / `server.preset_cases` / `server.CONFIG_PATH` / `evaluator._preset_labels` / `llm_client.SECRETS_FILE` / `test_attach`）一律经上述函数取值，禁止再引仓库根旧路径；注意 `.app_settings.json` 仍留在平台根（工作区解析依赖它，先有鸡才有蛋）。`/api/reveal` 由后端执行 `open -R`，因浏览器禁止 `http://` 跳 `file://`。

**质量评估（全维度模型判分）**：`core/eval_rubric.py` 是 20 项指标的单一事实源（分组/尺度/锚点），`core/evaluator.py` **只生产客观事实**（执行轨迹、工具成败、格式校验、稳定性一致率、安全扫描、成本估算 → `_objective_scores` / `_facts_payload`），**全部维度的分值由模型按锚点判定**（每用例一次 `core/llm_client.chat` 调用，OpenAI 兼容、地址自动补 `/v1`），由 `server.py` 的 `EvalState` 以后台任务跑，`web/index.html` 的「质量评估」页签呈现。

三条不可退让的约定：
1. **平台只送事实、不送分值**：本地算法仍算出同口径参考值，但只作为 `hint_score` 与事实包里的「本地参考值」用于对比模型判断、发现口径漂移；最终分一律以模型为准（档位之外的取值记为未评）。
2. **不适用性由本地判定**：无可视产物 → 美观度 N/A；无重复运行 → 稳定性 N/A；场景未分类 → 教学组与结果质量组都不评；成本控制只记录不评分。这些维度**不发模型**（模型看不到「没有的东西」，硬要它给分只会得到臆造值）。
3. **判分失败必须如实呈现**：调用失败时任何维度都不给分（安全性不得因「未命中红线」默认满分），根因（含供应商原文与来源 IP）经 `scores[].na_reason`、`judge_raw`、`summary.judge_errors` 全链路保真传递；`llm_used` 标记「模型侧是否真的产出」，送模型的维度零覆盖时 `overall_basis=objective_only`（综合分只剩硬规则分值，界面醒目提示）。`summary` 不再下发「客观均分 / 主观均分」两个字段，界面合并为一张「维度均分」卡片（与综合分同源，仅 5 分制刻度）：分值既然一律由模型判定，按维度性质分组只会得到两个重叠、不可加也不可比的均值。

**任务完成率的判据只有两条**：最终回复是否被截断、用户要求的产物是否真的产出。`core/eval_rubric.py` 的 `task_completion` 锚点就是判据本身（发给模型的唯一依据），`core/evaluator.py` 把这两条算成客观事实随请求下发，系统提示同步禁止按「内容是否切题 / 是否拒绝回答 / 轮次与工具成败」加减分。措辞必须写死 —— 一旦写成「答复是否完整 / 是否满足诉求」，模型就会引申成内容详尽度与拒答行为（实测两种越界判法都出现过）。

**安全红线硬覆盖**（`config.yaml` → `llm.redline_override`，默认 `true`）：命中「工具真的执行了破坏性命令」时强制安全性 0 分，覆盖模型判定并把模型原分记入 `evidence.model_score`。硬性的法律/伦理闸门不做模型单点（注入或误判放过真实危险行为的代价远高于一次假阳性）；引述/警示语境已在上游豁免，不会误伤正确拒绝。评估启动前用已存配置真实探活一次，不通过则拒绝启动且不写盘。

**用例意图的来源**：评估判定场景（教学/非教学，决定评哪一组）与「是否要求交付文件」（决定任务完成率、格式遵循度）时，只认用例自身声明的 `labels`；缺省时由 `core/case_intent.py` 从问题原文**基于显式证据**推断（产物要求需「产出动词 + 类型词」同句），并把来源（`labels` / `inferred` / `none`）与命中证据落盘、在界面标注「推断」。**不得按同名 id 借用预设标签** —— 界面手输用例的 id 是自动序号，与 `testcases.yaml` 的 id 必然重名，借了就会给无关问题凭空安上文件要求。

**用例附件投递**：被测应用的「引用本地文件」本质是**绝对路径列表**（前端取 Electron `File.path`，上限 10 个），其文件选择入口是 Electron 原生选择框（前端桥接 API `chooseLocalFile`，网页端兜底就是 `window.prompt` 问路径），**不走 Chromium 的 file chooser** —— 所以 `setFileInputFiles` / `setInterceptFileChooserDialog` 都够不着它。平台的做法：附件先上传到本地服务落盘（`artifacts/uploads/`，行内上传与附件库复用并存），运行时由 `drivers/ui_driver.attach_files()` 用 `Input.dispatchDragEvent`（dragEnter→dragOver→drop，带 `files`）**拖拽投递**，并以「附件按钮 title 的 `(n/10)` 计数是否真的增加」判定成败；计数没动即判失败，投递失败仍发送但写 `ui_error`（避免「附件没送进去却算跑通」的假通过）。

**安全红线的「引述 vs 执行」**：`core/safety_scan.py` 扫描最终答复、产出物文本与工具实参。命中分两类：**工具实参命中 = 试图执行，任何语气都不豁免**；散文（答复/产物）命中若整体处于拒绝/警示语境、且命中前没有「就要执行」的明确信号（`_EXEC_MARKERS`，刻意取窄）则**豁免** —— 不计红线，但记入 `evidence.noted_hits` 并交模型按 0/3/5 锚点判定。这层区分是必需的：拒绝执行时通常会**原文引用**那条危险命令来解释危害，只按正则命中判定的话，安全表现最好的回答反而会被判安全性 0 分（实测事故，见 `test_evaluator.py` 的 PASS 18）。

**配置集中点**：`config.yaml` 为唯一可调入口（应用路径与端口、环境档案、`auto_confirm` 参数、超时与并发、断言阈值、`expected_tools`、`llm.timeout_s` / `llm.temperature` / `llm.max_tokens` / **`llm.redline_override`**、`format_weights`）；合并优先级为 `config.yaml` 默认值 < 服务端密钥文件 `.llm_secrets.json` < 调用方显式传入（`core/evaluator.evaluate_round` 内合并）。**工作区缺少 `config.yaml` 时按仓库根 `config.template.yaml` 自愈初始化**（`core.settings.config_path()`，首次克隆/换机器开箱可用；此前缺失会让 `/api/env` 500 → 界面「环境」下拉变成没字、点不动的空框）。**应用路径与日志路径可由用户自配**（`core/settings.py`）：仪表盘「🖥 应用设置」写 `.app_settings.json`（0600、gitignore），合并优先级为 `.app_settings.json` > `config.yaml` > 内置默认（`DEFAULT_APP_BINARY` / `DEFAULT_SESSION_ROOT`，config.yaml 对应项留空即回退默认；内置默认**按平台取值**：macOS `/Applications/睿云智能工作台.app/Contents/MacOS/睿云智能工作台`、Windows `C:\Program Files\srtclaw\睿云智能工作台.exe`（`%ProgramFiles%`），`GET /api/app-settings` 另下发 `is_windows` / `defaults` 供界面回填 placeholder 与「留空使用默认」提示），`workspace_root` 缺省派生为 session_root 父目录；消费方（`run_pipeline` / `run_repro` / `discover_ui` / `server.load_cfg`）一律经 `effective_config` 取值，接口为 `GET/POST /api/app-settings`（空值保存=清除覆盖，`reset` 恢复默认）。`testcases.yaml` 仅作「导入预设」模板，不传 `--cases-file` 才回退执行。

**关键环境坑**：必须清除 `ELECTRON_RUN_AS_NODE`；必须加 `--disable-gpu --no-sandbox`；必须绕过系统代理；`session.messages.json` 整轮结束才落盘，不能用「N 秒无变化」判卡住；环境变量在启动时注入，故运行中环境选择器只读。

**Windows 启动/接入三坑（实测 2.0.14，均已修）**：①**应用已在运行但没有调试端口**（用户从开始菜单/自启动拉起）时，Electron 单实例会把新进程的 argv 转交给旧实例、新进程 `code=0` 秒退，调试端口永远不会打开 —— 直接 `Popen` 只会换来一句「应用启动失败或调试端口未就绪」（实测 22.5s 白等）。`launch()` 因此先按进程名检测无端口实例并关闭（`taskkill /T` 优雅 → 10s 超时后 `/F` 强制，本应用 helper 拒绝 WM_CLOSE，实际总会走强制），再以调试模式重启；不希望自动关闭时置 `app.restart_if_no_debug_port: false`。②**`tasklist` 输出编码随控制台代码页变化**（`chcp 65001` 下是 UTF-8，中文系统默认 GBK），按 locale 解码再比对中文进程名会得到 `鐫夸簯鏅鸿兘...` 乱码、进程恒查不到（这条曾让上面的检测静默失效）—— 进程名交给 `tasklist /FI`（内部按 Unicode 匹配），解析只按字节取「以引号开头的 CSV 行」的 pid 列。③**端口就绪 ≠ 界面可用**：dev 环境启动会先弹登录窗（`ruiyun-dev.3ren.cn`），主界面渲染明显晚于调试端口，旧实现用同一个 `launch_timeout_s=60` 等界面 → 「无法接入渲染进程」；现在界面就绪独立为 `app.ui_ready_timeout_s`（默认 180），`attach()` 失败时还会把端口上每个 page 的 url/title 打出来（区分「停在外壳/登录页」与「端口被别的程序占用」）。控制台解析一律别用 `text=True` + locale：`server.py` 起流水线时父子两端显式约定 UTF-8（`encoding="utf-8"` + `PYTHONIOENCODING`）。

**产物目录（轮次级入口）**：产物抽取在**流水线阶段**完成 —— `core/trajectory.build_case_detail(case, by_step, cfg)` 调 `core/artifacts.extract_artifacts(trace.tool_calls)`，随 `round_detail.json` 的 `cases[].artifacts`（含 `abs_path` 与 `artifacts_version`）与轮次级 `round_artifacts` 落盘，并进 `round_summary.json`。**不依赖质量评估**（评估仍独立抽一份用于判分）；抽取是纯日志解析，无网络无副作用。界面有两处入口：① 轮次列表元信息行的「📁 产物目录 N 件」（打开产物所在目录）；② **用例时间轴的末尾**，每件产物一个节点、**文件名即链接**（reveal 该文件 = 打开它所在目录并选中它），解析不到绝对路径的条目显示「本机未找到」且**不生成假链接**。数据由 `server._round_artifacts(d, evaluation, detail)` 下发 `{count,kinds,paths,root,exists,multi_dir,resolved,source}`：条目**优先取 `detail.cases[].artifacts`**（它一定经过版本校正），再退 `round_artifacts`、最后 `evaluation.cases[].artifacts`；目录取产物绝对路径的公共父目录（跨目录 `multi_dir=true`，文案为「最近公共目录」），无产物时回落 `evaluation.workspace_root`、再回落轮次归档目录。该字段必须同时出现在 `/api/rounds` 与 `/api/rounds/<id>`。

**产物抽取的三条硬规则（都踩过坑）**：①**失败的调用不算产物** —— 实测 `convert_markdown_to_docx` 返回 `{"success":false,"error":"File not found: 空白文档.docx.md"}`，旧实现照样记成一件产物，界面上就出现指向不存在文件的链接（`_call_failed()` 判定刻意保守：只有明确 `success:false`，或有 error 且无 `success:true` 才算失败）。②**脚本写出的文件要算产物** —— agent 常用 `mcp_exec_command`/python 脚本产出交付物（随后把临时脚本删掉），此时产出型工具可能压根没被调用或调用失败，但真实文件就在工作区里；`_artifacts_from_commands()` 只扫执行类/产出型工具的**参数文本**，取绝对路径且**必须真实存在**才记录，note 写明「按脚本/命令里出现的输出路径识别」，不做全工具扫描（否则 `read_file` 读过的文件会被误算成产物）。③**抽取规则会演进，老归档要能自愈** —— `EXTRACT_VERSION` 随规则一起写进 `round_detail.json`；`server._backfill_case_artifacts()` 发现版本落后就回到会话日志重算（结果按 `(run_id, 版本)` 缓存），否则旧轮次会永远带着当时抽错的清单。

**产物绝对路径的权威根是「应用自报」而非配置**：实测本机应用在工具结果里写的是 `workspace_path = C:\Users\<用户>\Documents\srtclaw\workspace`，与 `config.paths.workspace_root`（`~/.srtclaw/workspace` 派生）**不是同一个目录**。因此 `core.artifacts.resolve_abs_path()` 把 `full_path/workspace_path` 解析出的根**排在配置根之前**（该字段既可能是工作区目录、也可能是文件全路径，按「无后缀 或 本身是目录」判定），配置根只作为回落；兜底按文件名有界查找也先搜应用自报根。解析不到时**不隐藏条目**、不猜路径，界面显示「本机未找到」。

**Windows 资源管理器定位（`/api/reveal`，实测 2.0.14）**：①`explorer` 的 `/select,` 与路径**必须拆成两个参数**（`["explorer", "/select,", path]`）；连写成 `f"/select,{path}"` 时本机实测新窗口落在「文档」（`Shell.Application` 读到 `file:///C:/Users/<用户>/Documents`），即**打开位置完全错** —— 非 ASCII 路径（`睿云智能工作台.exe`）会让 explorer 把 `/select,PATH` 当成认不出的开关而退回默认目录。`tests/test_windows_compat.py` 已按此断言。②`explorer` 把请求转交给已运行实例后就退出，**退出码无意义**（成功也常返回 1），别用 `check=True`；启动走 `subprocess.Popen` 以便等它退出。③新窗口**不会自动来到前台**（实测 5s 后仍非前台，只在任务栏闪）：本机 `ForegroundLockTimeout=0x30d40`（200 秒），裸 `SetForegroundWindow` 必返回 False。有效手段实测两种：**AttachThreadInput**（附到当前前台线程 + 目标线程后 `SetForegroundWindow`/`SetFocus`）与**最小化再还原**（`SW_MINIMIZE`→`SW_RESTORE`）；`_focus_window()` 依次尝试，最后退回 `FlashWindow` 保证有可见反馈。④找窗口按 `CabinetWClass` 窗口类 + 调用前后差集，**不要按标题匹配** —— 标题随「隐藏已知扩展名」等设置变化（实测同一个 exe 所在目录的窗口标题可能显示成「文档」，这才是误导排查的根源）；`Shell.Application.Windows()` 的 `LocationURL` 才是真实位置，排查时用它对照。
