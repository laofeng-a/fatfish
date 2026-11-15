# PicoClaw 分叉现状与维护策略

长期独立维护、选择性移植修复，符合当前改动规模。新增实现已经大量放在自己的文件里，但 Telegram、消息入口和工具执行流程仍有较深改动。下一步值得先收拢这些入口的职责，再逐步减少需要人工对照的上游文件。单纯搬文件只能减少比对噪声，状态和行为之间的耦合还要单独处理。

## 比对口径

本报告核对于 2026-11-15，使用 Fatfish 提交 `4d7ed488a85ec78dcae5d3d967ec8c14ed1741b1` 的 `picoclaw/`。上游基线为 `2cf030d2fd3b871d7ec17e3be34c24688aac76da`，来自 [源码快照](source-snapshot.json)。本次实际拉取的 main 为 `bbf6893ca7afad27f1d00a0f5a45982a549c6ed6`，提交时间为 2026-08-19 17:17:32 UTC+8。统计以固定提交为准。

上游克隆放在仓库同级的 `server-setup/state/upstream-diff-20261115/upstream/`，工作区检出基线，`origin/main` 保留本次抓取版本。逐项比较双方 Git 已跟踪路径，先把 CRLF 归一为 LF，再用 `git diff --no-index --ignore-cr-at-eol --numstat` 统计增删行。未跟踪文件和工作区里的并行 Python 改动均不纳入。计数包含空行、注释和格式调整，不能直接当作新增功能的行数。

| 项目 | 文件数 | 行数 |
|---|---:|---:|
| 新增文件 | 84 | 10257 |
| 其中新增测试文件 | 47 | 5386 |
| 其余新增文件 | 37 | 4871 |
| 修改过的上游文件 | 42 | +1488 / -189 |
| 基线中有、本仓库未保留的路径 | 16 | 详见下文 |

## 修改过的上游文件

按实际 diff 人工分类，24 个涉及核心逻辑，12 个以接入和数据字段为主，5 个是测试，另有 1 个工程配置。接入点也会影响行为，这个分类只表示代码主要放在哪里。

| 相对 picoclaw/ 的文件 | 增加 | 删除 | 类型 | 变化 |
|---|---:|---:|---|---|
| [.gitignore](../picoclaw/.gitignore) | 1 | 1 | 工程配置 | 只让仓库根目录的 onboard 被忽略 |
| [pkg/agent/agent.go](../picoclaw/pkg/agent/agent.go) | 71 | 37 | 核心逻辑 | 回执消费者、消息续接、来源及自动回复条件 |
| [pkg/agent/agent_init.go](../picoclaw/pkg/agent/agent_init.go) | 7 | 0 | 接入为主 | 注入 ContextManager 和发送来源 |
| [pkg/agent/agent_mcp.go](../picoclaw/pkg/agent/agent_mcp.go) | 4 | 0 | 接入为主 | 向运行时登记已注册工具 |
| [pkg/agent/agent_message.go](../picoclaw/pkg/agent/agent_message.go) | 135 | 11 | 核心逻辑 | 后台领取、租约续期、完成确认及任务会话路由 |
| [pkg/agent/agent_outbound.go](../picoclaw/pkg/agent/agent_outbound.go) | 24 | 3 | 核心逻辑 | 错误留内部、自动最终文本和重复发送判断 |
| [pkg/agent/agent_steering.go](../picoclaw/pkg/agent/agent_steering.go) | 3 | 0 | 接入为主 | 建立本回合的发送登记 |
| [pkg/agent/agent_test.go](../picoclaw/pkg/agent/agent_test.go) | 3 | 3 | 测试 | 调整已有消息断言 |
| [pkg/agent/agent_utils.go](../picoclaw/pkg/agent/agent_utils.go) | 1 | 0 | 接入为主 | 标记自动最终文本的发送来源 |
| [pkg/agent/context.go](../picoclaw/pkg/agent/context.go) | 24 | 1 | 核心逻辑 | 人格正本、记忆来源及工具结果投影 |
| [pkg/agent/context_legacy.go](../picoclaw/pkg/agent/context_legacy.go) | 29 | 5 | 核心逻辑 | 旧 context manager 的 Fatfish 整理分支 |
| [pkg/agent/context_usage.go](../picoclaw/pkg/agent/context_usage.go) | 18 | 0 | 核心逻辑 | 按 Fatfish 实际输入计算用量与阈值 |
| [pkg/agent/instance.go](../picoclaw/pkg/agent/instance.go) | 2 | 0 | 接入为主 | 保存 ContextManager 引用及解释迭代配置 |
| [pkg/agent/pipeline_execute.go](../picoclaw/pkg/agent/pipeline_execute.go) | 70 | 16 | 核心逻辑 | 逐工具撤销检查、异步路由、发送和存储回执 |
| [pkg/agent/pipeline_finalize.go](../picoclaw/pkg/agent/pipeline_finalize.go) | 17 | 5 | 核心逻辑 | 决定最终文本是否存入聊天和对外发布 |
| [pkg/agent/pipeline_llm.go](../picoclaw/pkg/agent/pipeline_llm.go) | 90 | 5 | 核心逻辑 | 工具可见性、上下文准入、任务刷新、中断和错误重试 |
| [pkg/agent/pipeline_setup.go](../picoclaw/pkg/agent/pipeline_setup.go) | 31 | 9 | 核心逻辑 | 恢复点、Fatfish 预算和原始消息身份 |
| [pkg/agent/pipeline_streaming.go](../picoclaw/pkg/agent/pipeline_streaming.go) | 4 | 0 | 接入为主 | 调用统一判断以抑制内部或重复流式输出 |
| [pkg/agent/prompt_turn.go](../picoclaw/pkg/agent/prompt_turn.go) | 4 | 0 | 接入为主 | 图片续接的表达说明 |
| [pkg/agent/turn_coord.go](../picoclaw/pkg/agent/turn_coord.go) | 28 | 1 | 核心逻辑 | 后台让位、续接消息归属和写入错误传播 |
| [pkg/agent/turn_state.go](../picoclaw/pkg/agent/turn_state.go) | 36 | 1 | 核心逻辑 | 工具可见性、取消和消息身份状态 |
| [pkg/channels/telegram/telegram.go](../picoclaw/pkg/channels/telegram/telegram.go) | 292 | 22 | 核心逻辑 | 分批接收、媒体、发送闸门与真实消息回执 |
| [pkg/channels/telegram/telegram_test.go](../picoclaw/pkg/channels/telegram/telegram_test.go) | 215 | 0 | 测试 | Telegram 发送及接收回归用例 |
| [pkg/gateway/gateway.go](../picoclaw/pkg/gateway/gateway.go) | 15 | 3 | 核心逻辑 | 注册 5 秒到期探测及调整心跳结果处理 |
| [pkg/heartbeat/service.go](../picoclaw/pkg/heartbeat/service.go) | 57 | 15 | 核心逻辑 | 单循环执行、到期探测、最短分钟间隔及静默日志 |
| [pkg/heartbeat/service_test.go](../picoclaw/pkg/heartbeat/service_test.go) | 26 | 0 | 测试 | 分钟间隔与内部结论日志 |
| [pkg/memory/jsonl.go](../picoclaw/pkg/memory/jsonl.go) | 2 | 11 | 核心逻辑 | 改用能撤回短写的追加函数 |
| [pkg/providers/common/common.go](../picoclaw/pkg/providers/common/common.go) | 24 | 8 | 核心逻辑 | 保留模型网关的结构化上游错误 |
| [pkg/providers/openai_compat/provider.go](../picoclaw/pkg/providers/openai_compat/provider.go) | 34 | 1 | 核心逻辑 | 工具轮保留 reasoning_content，后台调用标记和检查 |
| [pkg/providers/openai_compat/provider_test.go](../picoclaw/pkg/providers/openai_compat/provider_test.go) | 23 | 0 | 测试 | reasoning_content 和工具历史 |
| [pkg/providers/protocoltypes/types.go](../picoclaw/pkg/providers/protocoltypes/types.go) | 23 | 10 | 接入为主 | 给消息增加持久身份及续接来源字段 |
| [pkg/session/jsonl_backend.go](../picoclaw/pkg/session/jsonl_backend.go) | 12 | 5 | 核心逻辑 | 写入错误传到 Save，保存消息身份 |
| [pkg/session/manager.go](../picoclaw/pkg/session/manager.go) | 1 | 0 | 接入为主 | 给完整消息补稳定身份 |
| [pkg/tools/cron.go](../picoclaw/pkg/tools/cron.go) | 33 | 2 | 核心逻辑 | 派生排程的撤销、任务路由及内部结果处理 |
| [pkg/tools/fs/filesystem.go](../picoclaw/pkg/tools/fs/filesystem.go) | 16 | 3 | 核心逻辑 | 原子写入遵循现有软链接并保留共享权限 |
| [pkg/tools/fs/load_image.go](../picoclaw/pkg/tools/fs/load_image.go) | 38 | 2 | 核心逻辑 | 图片登记、归一化工作进程及表达说明 |
| [pkg/tools/integration/mcp_tool.go](../picoclaw/pkg/tools/integration/mcp_tool.go) | 36 | 1 | 核心逻辑 | 可信调用上下文、阅读记录和发送回执归一 |
| [pkg/tools/integration/message.go](../picoclaw/pkg/tools/integration/message.go) | 5 | 2 | 接入为主 | 登记发送回调并提供回执说明 |
| [pkg/tools/integration/message_test.go](../picoclaw/pkg/tools/integration/message_test.go) | 6 | 2 | 测试 | 发送结果字段断言 |
| [pkg/tools/registry.go](../picoclaw/pkg/tools/registry.go) | 20 | 3 | 核心逻辑 | schema 到期后按已注册工具名执行，参数错误回传 schema |
| [pkg/tools/shared/result.go](../picoclaw/pkg/tools/shared/result.go) | 6 | 1 | 接入为主 | 增加 DeliverySummary 及发送结果说明 |
| [pkg/tools/shell.go](../picoclaw/pkg/tools/shell.go) | 2 | 0 | 接入为主 | 同步和后台命令继承 Fatfish 调用环境 |

## 新增文件

测试文件按 `_test.go` 识别。下面列出所有新增路径及其物理行数。

| 文件 | 行数 | 类别 |
|---|---:|---|
| [pkg/agent/background_yield.go](../picoclaw/pkg/agent/background_yield.go) | 52 | 实现或提示词 |
| [pkg/agent/background_yield_test.go](../picoclaw/pkg/agent/background_yield_test.go) | 117 | 测试 |
| [pkg/agent/context_fatfish.go](../picoclaw/pkg/agent/context_fatfish.go) | 543 | 实现或提示词 |
| [pkg/agent/context_fatfish_budget.go](../picoclaw/pkg/agent/context_fatfish_budget.go) | 146 | 实现或提示词 |
| [pkg/agent/context_fatfish_repair_test.go](../picoclaw/pkg/agent/context_fatfish_repair_test.go) | 130 | 测试 |
| [pkg/agent/context_fatfish_sample.go](../picoclaw/pkg/agent/context_fatfish_sample.go) | 42 | 实现或提示词 |
| [pkg/agent/context_fatfish_summary.go](../picoclaw/pkg/agent/context_fatfish_summary.go) | 284 | 实现或提示词 |
| [pkg/agent/context_fatfish_test.go](../picoclaw/pkg/agent/context_fatfish_test.go) | 312 | 测试 |
| [pkg/agent/delivery_history.go](../picoclaw/pkg/agent/delivery_history.go) | 63 | 实现或提示词 |
| [pkg/agent/delivery_history_test.go](../picoclaw/pkg/agent/delivery_history_test.go) | 187 | 测试 |
| [pkg/agent/fatfish_codex.go](../picoclaw/pkg/agent/fatfish_codex.go) | 187 | 实现或提示词 |
| [pkg/agent/fatfish_codex_test.go](../picoclaw/pkg/agent/fatfish_codex_test.go) | 229 | 测试 |
| [pkg/agent/fatfish_compaction.txt](../picoclaw/pkg/agent/fatfish_compaction.txt) | 3 | 实现或提示词 |
| [pkg/agent/fatfish_context_compact.go](../picoclaw/pkg/agent/fatfish_context_compact.go) | 241 | 实现或提示词 |
| [pkg/agent/fatfish_context_compact_test.go](../picoclaw/pkg/agent/fatfish_context_compact_test.go) | 200 | 测试 |
| [pkg/agent/fatfish_context_test.go](../picoclaw/pkg/agent/fatfish_context_test.go) | 156 | 测试 |
| [pkg/agent/fatfish_heartbeat_test.go](../picoclaw/pkg/agent/fatfish_heartbeat_test.go) | 216 | 测试 |
| [pkg/agent/fatfish_history_view.go](../picoclaw/pkg/agent/fatfish_history_view.go) | 37 | 实现或提示词 |
| [pkg/agent/fatfish_history_view_test.go](../picoclaw/pkg/agent/fatfish_history_view_test.go) | 164 | 测试 |
| [pkg/agent/fatfish_interrupt.go](../picoclaw/pkg/agent/fatfish_interrupt.go) | 10 | 实现或提示词 |
| [pkg/agent/fatfish_learning_test.go](../picoclaw/pkg/agent/fatfish_learning_test.go) | 71 | 测试 |
| [pkg/agent/fatfish_loops.go](../picoclaw/pkg/agent/fatfish_loops.go) | 182 | 实现或提示词 |
| [pkg/agent/fatfish_loops_test.go](../picoclaw/pkg/agent/fatfish_loops_test.go) | 165 | 测试 |
| [pkg/agent/fatfish_memory.go](../picoclaw/pkg/agent/fatfish_memory.go) | 137 | 实现或提示词 |
| [pkg/agent/fatfish_reform_audit_test.go](../picoclaw/pkg/agent/fatfish_reform_audit_test.go) | 313 | 测试 |
| [pkg/agent/fatfish_steering_test.go](../picoclaw/pkg/agent/fatfish_steering_test.go) | 157 | 测试 |
| [pkg/agent/fatfish_summary_prompt.txt](../picoclaw/pkg/agent/fatfish_summary_prompt.txt) | 14 | 实现或提示词 |
| [pkg/agent/fatfish_todos_on_demand_test.go](../picoclaw/pkg/agent/fatfish_todos_on_demand_test.go) | 93 | 测试 |
| [pkg/agent/fatfish_tool_loop_test.go](../picoclaw/pkg/agent/fatfish_tool_loop_test.go) | 64 | 测试 |
| [pkg/agent/fatfish_turn_event.go](../picoclaw/pkg/agent/fatfish_turn_event.go) | 70 | 实现或提示词 |
| [pkg/agent/fatfish_turn_event_test.go](../picoclaw/pkg/agent/fatfish_turn_event_test.go) | 142 | 测试 |
| [pkg/agent/history_origin.go](../picoclaw/pkg/agent/history_origin.go) | 21 | 实现或提示词 |
| [pkg/agent/internal_failure_test.go](../picoclaw/pkg/agent/internal_failure_test.go) | 37 | 测试 |
| [pkg/agent/internal_output_test.go](../picoclaw/pkg/agent/internal_output_test.go) | 40 | 测试 |
| [pkg/agent/output_isolation.go](../picoclaw/pkg/agent/output_isolation.go) | 51 | 实现或提示词 |
| [pkg/agent/output_isolation_test.go](../picoclaw/pkg/agent/output_isolation_test.go) | 180 | 测试 |
| [pkg/agent/runtime_recovery_test.go](../picoclaw/pkg/agent/runtime_recovery_test.go) | 83 | 测试 |
| [pkg/agent/self_managed_files_test.go](../picoclaw/pkg/agent/self_managed_files_test.go) | 43 | 测试 |
| [pkg/agent/upstream_failure.go](../picoclaw/pkg/agent/upstream_failure.go) | 136 | 实现或提示词 |
| [pkg/agent/upstream_failure_test.go](../picoclaw/pkg/agent/upstream_failure_test.go) | 201 | 测试 |
| [pkg/channels/telegram/emote_media.go](../picoclaw/pkg/channels/telegram/emote_media.go) | 144 | 实现或提示词 |
| [pkg/channels/telegram/emote_media_test.go](../picoclaw/pkg/channels/telegram/emote_media_test.go) | 146 | 测试 |
| [pkg/channels/telegram/fatfish_conversation.go](../picoclaw/pkg/channels/telegram/fatfish_conversation.go) | 145 | 实现或提示词 |
| [pkg/channels/telegram/fatfish_conversation_test.go](../picoclaw/pkg/channels/telegram/fatfish_conversation_test.go) | 139 | 测试 |
| [pkg/channels/telegram/live_emote_test.go](../picoclaw/pkg/channels/telegram/live_emote_test.go) | 51 | 测试 |
| [pkg/contextledger/agreement_archive_test.go](../picoclaw/pkg/contextledger/agreement_archive_test.go) | 166 | 测试 |
| [pkg/contextledger/material.go](../picoclaw/pkg/contextledger/material.go) | 157 | 实现或提示词 |
| [pkg/contextledger/material_test.go](../picoclaw/pkg/contextledger/material_test.go) | 101 | 测试 |
| [pkg/contextledger/source.go](../picoclaw/pkg/contextledger/source.go) | 320 | 实现或提示词 |
| [pkg/contextledger/store.go](../picoclaw/pkg/contextledger/store.go) | 529 | 实现或提示词 |
| [pkg/contextledger/store_test.go](../picoclaw/pkg/contextledger/store_test.go) | 326 | 测试 |
| [pkg/contextledger/summary.go](../picoclaw/pkg/contextledger/summary.go) | 592 | 实现或提示词 |
| [pkg/contextledger/summary_validate_test.go](../picoclaw/pkg/contextledger/summary_validate_test.go) | 254 | 测试 |
| [pkg/fatfish/codex.go](../picoclaw/pkg/fatfish/codex.go) | 54 | 实现或提示词 |
| [pkg/fatfish/codex_test.go](../picoclaw/pkg/fatfish/codex_test.go) | 25 | 测试 |
| [pkg/fatfish/media.go](../picoclaw/pkg/fatfish/media.go) | 42 | 实现或提示词 |
| [pkg/fatfish/output.go](../picoclaw/pkg/fatfish/output.go) | 22 | 实现或提示词 |
| [pkg/fatfish/profile_links.go](../picoclaw/pkg/fatfish/profile_links.go) | 68 | 实现或提示词 |
| [pkg/fatfish/profile_links_test.go](../picoclaw/pkg/fatfish/profile_links_test.go) | 77 | 测试 |
| [pkg/fatfish/runtime.go](../picoclaw/pkg/fatfish/runtime.go) | 128 | 实现或提示词 |
| [pkg/fatfish/runtime_test.go](../picoclaw/pkg/fatfish/runtime_test.go) | 25 | 测试 |
| [pkg/heartbeat/due_checker_test.go](../picoclaw/pkg/heartbeat/due_checker_test.go) | 119 | 测试 |
| [pkg/heartbeat/self_managed_files_test.go](../picoclaw/pkg/heartbeat/self_managed_files_test.go) | 33 | 测试 |
| [pkg/memory/jsonl_append.go](../picoclaw/pkg/memory/jsonl_append.go) | 41 | 实现或提示词 |
| [pkg/memory/jsonl_append_test.go](../picoclaw/pkg/memory/jsonl_append_test.go) | 59 | 测试 |
| [pkg/memory/retire_prefix.go](../picoclaw/pkg/memory/retire_prefix.go) | 45 | 实现或提示词 |
| [pkg/memory/retire_prefix_test.go](../picoclaw/pkg/memory/retire_prefix_test.go) | 35 | 测试 |
| [pkg/providers/common/upstream_error_test.go](../picoclaw/pkg/providers/common/upstream_error_test.go) | 18 | 测试 |
| [pkg/providers/openai_compat/fatfish_replay_test.go](../picoclaw/pkg/providers/openai_compat/fatfish_replay_test.go) | 28 | 测试 |
| [pkg/session/history_identity.go](../picoclaw/pkg/session/history_identity.go) | 25 | 实现或提示词 |
| [pkg/session/retire_prefix.go](../picoclaw/pkg/session/retire_prefix.go) | 46 | 实现或提示词 |
| [pkg/session/storage_error_test.go](../picoclaw/pkg/session/storage_error_test.go) | 40 | 测试 |
| [pkg/tools/fatfish_exec_env.go](../picoclaw/pkg/tools/fatfish_exec_env.go) | 36 | 实现或提示词 |
| [pkg/tools/fatfish_exec_env_test.go](../picoclaw/pkg/tools/fatfish_exec_env_test.go) | 40 | 测试 |
| [pkg/tools/fatfish_loop_cron.go](../picoclaw/pkg/tools/fatfish_loop_cron.go) | 74 | 实现或提示词 |
| [pkg/tools/fatfish_loop_cron_test.go](../picoclaw/pkg/tools/fatfish_loop_cron_test.go) | 79 | 测试 |
| [pkg/tools/fs/profile_atomic_write_test.go](../picoclaw/pkg/tools/fs/profile_atomic_write_test.go) | 103 | 测试 |
| [pkg/tools/integration/fatfish_codex_test.go](../picoclaw/pkg/tools/integration/fatfish_codex_test.go) | 42 | 测试 |
| [pkg/tools/integration/fatfish_delivery.go](../picoclaw/pkg/tools/integration/fatfish_delivery.go) | 121 | 实现或提示词 |
| [pkg/tools/integration/fatfish_delivery_test.go](../picoclaw/pkg/tools/integration/fatfish_delivery_test.go) | 61 | 测试 |
| [pkg/tools/integration/fatfish_mcp_test.go](../picoclaw/pkg/tools/integration/fatfish_mcp_test.go) | 20 | 测试 |
| [pkg/tools/integration/reply_delivery_test.go](../picoclaw/pkg/tools/integration/reply_delivery_test.go) | 41 | 测试 |
| [pkg/tools/registry_deferred_execution_test.go](../picoclaw/pkg/tools/registry_deferred_execution_test.go) | 58 | 测试 |
| [pkg/tools/shared/reply_delivery.go](../picoclaw/pkg/tools/shared/reply_delivery.go) | 63 | 实现或提示词 |

## 删除及替换的部分

目录差异中的 16 个缺失路径由 15 个上游 `.github` 配置及一份开发工具错误记录组成。发布 CI、依赖机器人、issue 模板等没有随导入快照保留，当前仓库根目录也没有自己的 `.github/`。它们影响项目维护自动化，不能算成删除了 16 个运行时功能。

- `.github/FUNDING.yml`
- `.github/ISSUE_TEMPLATE/bug_report.md`
- `.github/ISSUE_TEMPLATE/feature_request.md`
- `.github/ISSUE_TEMPLATE/general-task---todo.md`
- `.github/dependabot.yml`
- `.github/pull_request_template.md`
- `.github/workflows/build.yml`
- `.github/workflows/create-tag.yml`
- `.github/workflows/create_dmg.yml`
- `.github/workflows/docker-build.yml`
- `.github/workflows/nightly.yml`
- `.github/workflows/pr.yml`
- `.github/workflows/release.yml`
- `.github/workflows/stale.yml`
- `.github/workflows/upload-tos.yml`
- `pkg/seahorse/.omc/state/last-tool-error.json`

运行时代码中，Fatfish 启用时改用自己的上下文账本、记忆投影和摘要流程。旧 context manager 的实现仍在，`context.go` 中的默认记忆注入在 Fatfish 路径被替代。后台自动最终文本和部分自动错误消息改为内部记录，发送走显式工具及回执。这些是行为替换，不能用文件删除数表达。其他上游渠道和 launcher 的源码仍保留，是否用于具体部署应另查运行配置。

## 上游基线之后的变化

本次 main 比基线多 **50 个可达提交**，其中 **19 个在 first-parent 主线上、31 个为非 merge 提交**。这是 Git 图上的差集，某些分支提交的作者或提交日期早于基线日期。全部提交信息已核对，重点条目如下。官方 release 页的 v0.3.1 和 nightly 仍指向基线，单看 release notes 会漏掉这 50 个提交。[固定范围](https://github.com/sipeed/picoclaw/compare/2cf030d2fd3b871d7ec17e3be34c24688aac76da...bbf6893ca7afad27f1d00a0f5a45982a549c6ed6)，[release 页](https://github.com/sipeed/picoclaw/releases)。

| 修复或变化 | 涉及文件及本地重叠 | 建议 |
|---|---|---|
| `49183d7` 为通过 govulncheck 更新 Go 和 x/text。main 当前要求 Go 1.25.13、x/text 0.39.0，另有 x/crypto 0.53.0 更新。本地是 Go 1.25.11、x/text 0.37.0、x/crypto 0.51.0 | go.mod、go.sum，本地相对基线未改，文件不重叠 | 优先独立评估依赖更新及 govulncheck。提交信息足以说明安全相关维护意图，具体可达漏洞仍需扫描确认。[提交](https://github.com/sipeed/picoclaw/commit/49183d7e8daed0dba89ddbb6fcb60089401d9680) |
| launcher setup 的跨站请求检查 `f5b2ce7` | web/backend/api/auth.go，本地未改 | 启用 launcher 的实例应优先评估。现有交接未记载该入口的使用情况。[提交](https://github.com/sipeed/picoclaw/commit/f5b2ce748210ced16b7f7fb9001737366c828137) |
| Anthropic 重新加载历史后，ToolCall 的 Name/Arguments 丢失导致 tool_result 无匹配项，`994c0ae` 改为从 Function 恢复 | anthropic/provider.go、anthropic_messages/provider.go 及测试，本地未改 | 使用这些 provider 时适合单独移植，带上反序列化历史的测试。[提交](https://github.com/sipeed/picoclaw/commit/994c0aea1151ccd9a56ac7279cc2fd20588594a5) |
| `/clear` 清除实际路由 agent 的会话，`c4fb7a2`、`ffffb6a` | context_legacy.go、agent_test.go 与本地重叠，另改 agent_command.go、context_seahorse.go、context_manager_test.go | 应验证 Fatfish 的 Clear 实现如何找到会话所属 agent，再挑选性移植。[提交](https://github.com/sipeed/picoclaw/commit/ffffb6a0cb98ea264c4842b54da67d4551b7cb53) |
| write_file 的说明区分整文件覆盖，并只推荐已注册的 append/edit 工具，`8f891d5`、`a680d8f` | filesystem.go、instance.go 与本地重叠，测试文件未改 | 可单独评估，保留本地软链接和共享权限写入行为。[提交](https://github.com/sipeed/picoclaw/commit/a680d8fcfaf1551f4188f9a70ad97261a3044747) |
| Windows 文件系统相对路径分隔符修复，`acc2c5c`，并入 `79ae6bf` | filesystem.go 与本地重叠 | 主要对 Windows 上运行 Go 文件工具有意义，现有 Linux 服务优先级较低。[提交](https://github.com/sipeed/picoclaw/commit/acc2c5c6aad4296aadcdae4777727a67688dedfc) |
| 自定义 exec allow/deny 组合语义修复，`9721c36`、`ff247b6` | shell.go 与本地重叠，Fatfish 只在运行命令处加环境接入 | 属于上游通用策略兼容性变化。jg 已确认的权限设计继续作为需求，保留当前策略。[提交](https://github.com/sipeed/picoclaw/commit/ff247b63ac815dce10c0a799caed53194d8193ac) |
| web、LINE 的 Body.Close 错误显式忽略，前端依赖、DeltaChat、token usage、模型 fallback 等 | 部分 Agent/gateway 文件与本地重叠 | Close 的条目主要是错误处理写法调整，不能据标题当作新的连接泄漏修复。其余按实际需要评估，不随修复整批带入。[main](https://github.com/sipeed/picoclaw/commit/bbf6893ca7afad27f1d00a0f5a45982a549c6ed6) |

公开安全公告页当次显示没有已发布条目。它不能代替提交检查和依赖扫描。[安全公告](https://github.com/sipeed/picoclaw/security/advisories)。

基线到 main 的全部文件变化，与本地修改过的上游文件共有下列 **10 个重叠路径**。上表中的单项修复还需按它自己的 diff 判断冲突，不能因为出现在总体交集中就认定每个修复都冲突。

- `pkg/agent/agent_test.go`
- `pkg/agent/context_legacy.go`
- `pkg/agent/instance.go`
- `pkg/agent/pipeline_llm.go`
- `pkg/agent/pipeline_streaming.go`
- `pkg/agent/turn_coord.go`
- `pkg/agent/turn_state.go`
- `pkg/gateway/gateway.go`
- `pkg/tools/fs/filesystem.go`
- `pkg/tools/shell.go`

## 维护策略

建议按长期独立分叉维护，默认每月查看一次上游提交和依赖变化，每季度做一次较完整的差异复核。评估后可以结论为本期无需移植。保留外部上游克隆及已评估的 commit，让每次检查有明确起点。

收拢代码的候选按收益和改动范围排序如下。

1. **Telegram 接收与发送适配。** telegram.go 有 +292/-22，另有自己的 conversation/media 文件。可逐步把分批接收、发送前后回执处理收进 Fatfish 适配文件，主文件只调用入口。测试覆盖文本、贴纸、图片、部分发送和失败回执。
2. **后台心跳入口。** agent_message.go 有 +135/-11，主要来自后台领取、续租、完成确认和上下文路由。可先迁入专用文件，保留 ProcessHeartbeat 接入，再按场景审计状态。测试覆盖真人占用、租约、安静结束和撤销。
3. **模型与工具执行流程。** pipeline_llm.go +90/-5、pipeline_execute.go +70/-16，分别串起预算、任务、取消、结果和回执。应先明确每一步的输入输出与唯一责任，再考虑提取函数，减少同一决策散在多个阶段。测试保留当前工具协议、已完成动作和迟到结果。
4. **保留小而明确的公共修复。** 消息身份字段、JSONL 短写回滚、沿软链接写正本等改动与上游通用对象紧密相关。为了少几个改动文件再包一层，会增加理解成本，可以保持。

选择性移植时以单个事故或修复组为单位，读完整 diff 和测试。无共同本地历史时，可以在外部克隆里导出 patch 再人工适配，不直接合并整个 main。涉及已改文件时，保留 Fatfish 的状态语义；不重叠的文件也要验证调用者和依赖。

## 下次检查步骤

1. 记录 Fatfish HEAD 和上次已评估的上游 SHA。先确认工作区归属，只读分析使用已提交内容。
2. 在仓库外克隆中 fetch，锁定本次 origin/main 的完整 SHA。用可达提交差集统计，不按日历日期过滤。
3. 同时读提交、release notes 和公开安全公告，优先检查依赖及真实影响当前 provider、会话和存储的修复。
4. 用已跟踪路径和 LF 归一化方法重算新增、修改、缺失清单，并与候选修复的文件集取交集。
5. 对每项写明是否适用、是否已修过、是否移植及验证方法。测试与部署应另开任务确认。
6. 更新本文件的评估 SHA 和结果。服务器实际版本仍由 DEPLOYED.md 和当次核对记录说明。

可复用的命令如下，在 Fatfish 仓库根目录运行。首次克隆使用新的外部目录。

```powershell
$auditDir = '..\server-setup\state\upstream-diff-YYYYMMDD'
git clone --filter=blob:none --no-checkout https://github.com/sipeed/picoclaw.git "$auditDir\upstream"
git -C "$auditDir\upstream" checkout --detach 2cf030d2fd3b871d7ec17e3be34c24688aac76da
git -C "$auditDir\upstream" rev-parse origin/main
git -C "$auditDir\upstream" log --oneline --first-parent 2cf030d2fd3b871d7ec17e3be34c24688aac76da..origin/main
git -C "$auditDir\upstream" diff --name-only 2cf030d2fd3b871d7ec17e3be34c24688aac76da origin/main
git diff --no-index --ignore-cr-at-eol --numstat -- "$auditDir\upstream\pkg\agent\agent.go" .\picoclaw\pkg\agent\agent.go
```

最后一条命令在有差异时退出码为 1。完整比对要遍历双方已跟踪文件，避免把上游克隆的 .git 或临时文件算进来。本次逐文件统计、原始 patch 和提交列表保存在仓库同级 `server-setup/state/upstream-diff-20261115/`。
