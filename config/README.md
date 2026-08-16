# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序前先完成本机 Codex 登录。PA Agent 的两阶段分析、自由对话和重试调用统一使用 **Codex SDK**，无需 Base URL 或 API Key。

   设置页只保留 Codex 模型信息；保存时会清除旧配置中的第三方 Base URL 和模型 API Key。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

4. 如需自定义 TradingView 品种别名，复制模板：

   ```cmd
   copy config\tv_symbol_aliases.example.json config\tv_symbol_aliases.json
   ```

## `settings.json` 字段说明

配置统一保存在 `settings.json`；量化选股阈值位于 `stock_selection`，不在代码中散落维护。

### provider — AI 提供商

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider.backend` | string | `"codex_sdk"` | 固定为 Codex SDK；旧提供商值在加载时自动迁移 |
| `provider.model` | string | `"gpt-5.6-terra"` | Codex SDK 使用的模型；默认 Terra |
| `provider.base_url` | string | `""` | 固定为空；Codex SDK 不使用第三方模型网关 |
| `provider.api_key` | string | `""` | 固定为空；认证来自本机 Codex 登录状态 |
| `provider.api_key_encrypted` | string | `""` | 固定为空；PA Agent 不保存模型凭据 |
| `provider.thinking` | bool | `true` | 是否启用思考/推理类扩展参数（依模型与网关而定）。关闭可 3–5 倍提速但分析质量下降 |
| `provider.reasoning_effort` | string | `"high"` | 推理深度：`low` / `medium` / `high` / `max` |
| `provider.context_window` | int | `2000000` | 用于上下文占用提示的窗口大小（tokens） |
| `provider.codex_process.hide_console_on_windows` | bool | `true` | Windows 下隐藏 Codex app-server 控制台窗口；设为 `false` 可用于子进程调试 |

### general — 通用设置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `general.investment_scope` | string | `"a_share_only"` | 生产分析与投资范围固定为A股股票；指数只作为四层评分输入，不作为直接投资标的 |
| `general.last_data_source` | string | `"eastmoney"` | 生产K线数据源固定为东方财富A股；其他旧数据源仅保留历史兼容代码，不在生产界面开放 |
| `general.last_tradingview_exchange` | string | `""` | TradingView 交易所。空字符串 =（自动）依次探测预设列表。如 `OANDA`、`SSE`、`HKEX` 等 |
| `general.last_symbol` | string | `"600519"` | 默认A股股票代码；非A股、指数、基金、债券等输入会被生产入口拒绝 |
| `general.last_timeframe` | string | `"15m"` | 默认周期，如 `1m`、`5m`、`15m`、`1h`、`4h`、`1d` |
| `general.analysis_bar_count` | int | `100` | 提交分析时使用的 K 线数量（2–5000） |
| `general.refresh_interval_ms` | int | `1000` | 图表自动刷新间隔（毫秒） |
| `general.context_warning_threshold_pct` | float | `80.0` | 上下文占用警告阈值（百分比） |
| `general.decision_stance` | string | `"balanced"` | 阶段二交易倾向：`conservative` / `balanced` / `aggressive` / `extreme_aggressive` |
| `general.incremental_max_new_bars` | int | `10` | 增量分析触发阈值：新增已收盘 K 线 ≤ 此值时自动走增量模式（0–500） |
| `general.auto_resume_chart_after_analysis` | bool | `false` | 分析结束后是否自动恢复「图表实时更新」 |
| `general.keep_analysis` | bool | `false` | 持续跟踪分析：新 K 线收盘时自动触发新一轮分析 |
| `general.cancel_keep_analysis_on_retry` | bool | `false` | 校验失败触发重试后自动关闭 `keep_analysis` |
| `general.alert_on_order_opportunity` | bool | `true` | 阶段二给出交易方案时播放警报音、弹窗提示，并自动切换到「决策」页 |
| `general.decision_flow_auto_play` | bool | `true` | 决策树可视化自动播放 |
| `general.decision_flow_play_seconds` | int | `50` | 决策树可视化自动播放时长（秒） |
| `general.decision_flow_default_zoom_pct` | int | `600` | 决策树可视化默认缩放百分比（≥10） |
| `general.stream_pane_font_pt` | int | `11` | 「实时」页等宽字体字号（pt，8–28） |
| `general.chart_seq_label_font_pt` | int | `11` | K 线图上序号标签的字号（pt，6–24） |

### prompt — Prompt 组装调优

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt.stage2_load_full_strategy_library` | bool | `false` | 阶段二是否加载全部 22 个策略文件（通常仅路由匹配的策略文件） |
| `prompt.experience_max_entries` | int | `3` | 经验库最大加载条目数（0–10） |
| `prompt.experience_max_chars_per_entry` | int | `400` | 每条经验最大字符数（100–4000） |
| `prompt.stage1_inject_pattern_briefs` | bool | `true` | 阶段一是否注入模式判定表和速查 brief（减少 missed tags） |

### portfolio_risk — 组合风控

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `portfolio_risk.monthly_warning_loss_pct` | float | `1.0` | 当月收益达到 `-1%` 后，只允许最高等级四层信号继续进入组合风控 |
| `portfolio_risk.highest_grade_score` | float | `80.0` | 月度亏损警戒后的最低综合分；仅适用于四层评分新增策略 |
| `portfolio_risk.monthly_stop_loss_pct` | float | `1.5` | 当月收益达到 `-1.5%` 后停止全部新增仓位，只管理退出 |
| `portfolio_risk.live_trading_enabled` | bool | `false` | 实盘总开关；回测和影子验证完成前保持关闭 |

### stock_selection — A股智能选股

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `stock_selection.refresh_seconds` | `900` | 后台选股刷新周期；手工点击“重新扫描”可立即运行 |
| `stock_selection.seed_per_channel` | `18` | 涨幅、成交额、低量比三个全A种子通道各自读取数量 |
| `stock_selection.hotspot_scan_limit` | `24` | 每轮进一步核验题材、资金和公告的最大股票数 |
| `stock_selection.hot_theme_min_percentile` | `75.0` | 近期热点题材的板块相对强度最低分位 |
| `stock_selection.hot_theme_min_persistence_days` | `2` | 热点最少持续交易日 |
| `stock_selection.main_force_min_net_inflow_pct` | `0.5` | 主力关注题材的板块主力净流入占比下限 |
| `stock_selection.volume_suffocation_max_ratio` | `0.65` | 近5日均量/此前20日均量上限 |
| `stock_selection.atr_contraction_max_ratio` | `0.8` | 近5日ATR/此前20日ATR上限 |
| `stock_selection.range_contraction_max_ratio` | `0.8` | 近5日振幅/此前20日振幅上限 |
| `stock_selection.trend_min_volume_ratio` | `1.2` | 趋势突破日相对20日均量的最低量比 |
| `stock_selection.require_no_major_negative` | `true` | 必须通过重大负面公告硬过滤；缺失或无法核验时不入选 |

智能选股只生成观察候选。加入监控池后仍进入独立股票沙箱；池外股票继续走
`manual_exception_4321_v1`，选股结果不会直接生成订单或解除风控。

### validation — 校验与重试

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `validation.normalization_mode` | string | `"lenient"` | 归一化模式：`strict`（严格拒绝异常值）/ `lenient`（容忍轻微偏差） |
| `validation.stage1_coherence_checks` | bool | `false` | 阶段一跨字段一致性检查（闸门 trace、逐 K 摘要、模式标签等） |
| `validation.stage2_coherence_checks` | bool | `false` | 阶段二诊断与 trace 交叉检查 |
| `validation.trace_semantic_checks` | bool | `false` | 语义一致性检查（方向/信号逻辑冲突检测） |
| `validation.strict_bar_by_bar_features` | bool | `false` | 严格逐 K 特征校验（开启后对特征字段做严格验证） |
| `validation.disable_truncation_repair` | bool | `false` | 禁用流式 JSON 截断尾部修复 |
| `validation.retry_enabled` | bool | `true` | 校验失败时是否自动重试 |
| `validation.retry_max` | int | `3` | 格式错误（category a）最大重试次数（0–5） |
| `validation.retry_max_semantic` | int | `1` | 语义错误（category c）最大重试次数（0–3） |
| `validation.retry_stage2` | bool | `true` | 阶段二校验失败时是否重试 |

## 安全提醒

- **不要**将 `config/settings.json`、`config/exception_state.json`、`config/tv_symbol_aliases.json` 提交到 Git。
- 若曾误提交 API Key，请立即在服务商处**作废并轮换**密钥。
- 建议在仓库根目录执行：`powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1`
