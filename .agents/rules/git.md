# Git 提交规范

## 语言要求
- 所有的 `git commit` 提交信息**必须一律使用中文**编写，严禁使用英文作为提交说明。

## 提交信息格式
遵循约定式提交（Conventional Commits）风格：
`<type>: <中文简短描述>`

常用类型（type）：
- `feat`: 新增功能 / 特性
- `fix`: 修复 Bug 或异常
- `refactor`: 代码重构（不改变功能与 API）
- `style`: 样式调整、排版、UI 布局微调
- `perf`: 性能优化
- `test`: 单元测试、集成测试相关变动
- `docs`: 文档、说明或注释更新
- `chore`: 构建、依赖、工程配置或工具调整

## 示例
- `feat: 支持 Excel 用例批量导入与列映射预览`
- `refactor: 将 index.html 拆解为原生 ESM 模块与独立 CSS 文件`
- `fix: 解决历史记录详情面板滚动穿透问题`
- `style: 优化控制台拖拽手柄悬停动效与高度切换`
