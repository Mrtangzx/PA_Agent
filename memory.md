# 项目关键信息

- 项目名称：PA Agent（Price Action AI K 线分析桌面端）
- 技术栈：Python 3.11+、PyQt6、pyqtgraph；依赖由 `pyproject.toml` 和 `uv.lock` 管理。
- 推荐本地启动：在项目根目录执行 `uv run python run.py`。
- 主入口：`run.py`，实际 GUI 入口为 `pa_agent.main:main`。
- 本地配置：运行时配置位于 `config/`；仓库仅提交 `*.example.json`，密钥和个人配置不应提交。
- 日志目录：`logs/`，重点查看 `logs/pa_agent.log` 与 `logs/crash.log`。
- 应用性质：本地桌面 GUI，不提供固定 HTTP 端口；运行验收以 GUI 进程、主窗口和启动日志为准。
- AI 后端：支持 OpenAI 兼容 API、Cursor SDK 和官方 `openai-codex` Python SDK；Codex 路由使用本机登录、临时线程和只读沙箱。

## 维护记录

- 2026-08-11：首次在当前工作区进行本地启动检查并创建本文件。
- 2026-08-11：集成 Codex SDK Agent 分析后端，默认 Codex 模型为 `gpt-5.6-terra`。

## 量化交易设计原则

> 不要急着增加更多策略，也不要急着为每个品种单独调参；先修复止损逻辑、建立交易结果闭环和独立风险层，然后让真实样本决定哪些品种、周期和市场状态值得交易。
