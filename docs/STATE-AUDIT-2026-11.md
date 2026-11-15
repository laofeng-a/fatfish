# 按场景审计状态与规则

优先精简摘要材料的重复约定、已经没有业务判断的 RPC 和过时修正分支，再处理已证实的活动续记有效期问题。后台发送与额度排程适合分阶段收拢。任务版本、活动 epoch、发送回执和执行租约各有用途，保留这些边界更符合现有事故证据。

## 审计范围与证据口径

审计基于已提交源码 `ce99e4829063051dafcb95d9199d9345e939ca3d`，包括 11 月 14 日的约定退场实现。本次从 Git 导出隔离副本进行源码检查和已有离线用例验证。配置示例与部署记录引用 11 月 14 日交接和 [DEPLOYED.md](DEPLOYED.md)，它们表示各自记录时点的状态。

下文登记的是六类场景的状态转换、准入、恢复和发送规则。表中的“事故”指该规则要防的具体后果，按以下三类标注。测试通过只证明指定输入的行为，不算自然发生过事故。

- **真实事故**，已有发布记录、诊断或回执支持该事故发生。注明历史问题是否已修复。
- **理论风险**，代码或需求能说明目的，但现有材料只见设计或隔离测试。产品明确要求的安静结束、显式安排等边界也归这一类。
- **查不到来由**，检索导入后的 Git 历史、发布文档及指定 WORKLOG，仍找不到具体事故或有效判断。这个分类本身不等于应删除。

证据编号如下，私人来源只记录问题类别和出处。

| 编号 | 证据与边界 |
|---|---|
| E1 | 11 月 14 日摘要修复 WORKLOG 记载出处失效、原样保留和 revision 检查互相牵制。61–78 号候选全部被拒；修复后离线回放 16 份中 14 份通过，另外两份超长。出处归一、程序补约定及 Rebase 已修复这些机制。仓库外 `server-setup/state/summary-home-fix-20261114/WORKLOG.md` 第 5–6、45–49 行，源码提交 `848ca9e`。 |
| E2 | 真人短消息被误当后台事件，真实输入和触发记录已核对。来源标识修复已有发布和回放记录。[来源修复](RELEASE-2026-11-05-TURN-SOURCE.md)。 |
| E3 | 任务盯守撤销后派生闹钟继续运行、旧请求占住新指令、后台延迟让位，均有 11 月 5 日现场诊断和后续修复。现有机制已补齐。[运行恢复](RELEASE-2026-11-06-RUNTIME-RECOVERY.md)。仓库外 `server-setup/state/afternoon-loops-diagnosis-20261105.md` 的时间线及 A–G 节。 |
| E4 | 已知延迟工具在 schema TTL 到期后报 tool not found，属于实际执行入口故障。执行与 schema 可见性已分离。[工具修复](RELEASE-2026-11-04-PROFILE-TOOLS.md)。同轮 schema 消失的衔接问题也见 E3。 |
| E5 | 实际磁盘写满、EOF 和 JSONL 损坏，后续修复 1676 条记录并保留原件。存储失败传播和短写撤回有实际事故来由。[存储与回执恢复](RELEASE-2026-11-06-RUNTIME-RECOVERY.md)。 |
| E6 | 已有自然运行的 chat_armed、chat_claimed、chat_finished 和真实发送 ID。静默、失败、旧来源与迟到回执的分支主要由隔离测试验证。[陪聊发布](RELEASE-2026-11-06-COMPANION-CADENCE.md)。其中 120 秒及窗口约束是历史版本，当前规则以源码和 DESIGN 为准。 |
| E7 | 11 月 8 日自由活动沿用前一日短期静默续记，且把续记时间理解到错误日期。属于交接中已经定位的问题。仓库外 `server-setup/HANDOFF-FATFISH-20261114.md` 第 4 节及 jg 提供的项目交接。 |
| E8 | 合并输入的旧 received 回执造成误判忙碌，已去掉这项推断。仓库外 `server-setup/state/companion-cadence-20261106/WORKLOG.md` 第 9 行。 |
| E9 | 11 月 5 日独立 cron 曾发出内部结论，派生入口的最终文本隔离随后补齐。仓库外 `server-setup/state/afternoon-loops-diagnosis-20261105.md` 的 14:00 时间线及 H 节，修复结果见 E3。 |
| E10 | 约定退场是 11 月 14 日明确的产品方案，首次通过展示、下一次通过归档。已有终态、拒绝和 active 恢复隔离测试。当前记录中未见终态约定自然退场事故。[部署与验证范围](DEPLOYED.md)，源码提交 `d350869`。 |

## 共享的执行和发送状态

这些状态被多个场景共用。Go 的执行位置、Python 的持久租约和 Telegram 的发送结果表示不同阶段。

| 状态及位置 | 改变它的入口和含义 |
|---|---|
| Go 内存 `activeTurnStates`、`workerSem`、`providerCancel`、`exec.pendingMessages` | 会话预占和工作位置在运行开始建立，结束释放；新真人消息进入 steering，能取消当前模型生成或可中断读取。[agent.go:147](../picoclaw/pkg/agent/agent.go#L147)，[turn_state.go:586](../picoclaw/pkg/agent/turn_state.go#L586) |
| runtime.sqlite 的 `wakes.id/created/heartbeat_at/finished/contacted/kind`，`state.current_wake`，`loop_wakes.id/task_id/epoch/kind` | 领取时记执行身份与用途，15 秒续租，结束确认 finished。contacted 记录确有发送，loop_wakes 记录业务归属。[runtime_policy.py:356](../server-setup/runtime_policy.py#L356)，[runtime_policy.py:622](../server-setup/runtime_policy.py#L622)，[runtime_policy.py:625](../server-setup/runtime_policy.py#L625) |
| runtime.sqlite 的 `sends.ticket/status/fingerprint/wake/message_ids`、`send_actions`、`send_turn_attempts`；Go `replyDeliveryState` | outbound 先给票据，实际渠道回传 sent/partial/rejected/unknown；分段记录留在 send_actions。Go 的本轮登记表示发送工具已接手，实际送达仍看渠道回执。[runtime_policy.py:459](../server-setup/runtime_policy.py#L459)，[runtime_policy.py:528](../server-setup/runtime_policy.py#L528)，[reply_delivery.go:9](../picoclaw/pkg/tools/shared/reply_delivery.go#L9) |
| Go HeartbeatService 的 interval/dueEvery/enabled/executing/stopChan，运行配置的 HeartbeatConfig.Interval | 常规分钟心跳与 5 秒到期探测共用执行入口，探测读取 chat_due，实际领取统一走 Runtime.wake。[service.go:38](../picoclaw/pkg/heartbeat/service.go#L38)，[gateway.go:871](../picoclaw/pkg/gateway/gateway.go#L871) |
| 上下文中的 background 标记、session_key、message_id、delivery_origin，及 `internal-output.jsonl` | 区分自动最终文本、显式工具发送与内部记录，多个发布入口会读取这些字段。[output_isolation.go:16](../picoclaw/pkg/agent/output_isolation.go#L16)，[runtime_policy.py:459](../server-setup/runtime_policy.py#L459) |

共享规则登记如下。

| 规则 | 状态转换或判断 | 防的事故与证据分类 | 代码入口 |
|---|---|---|---|
| G1 真人优先、会话与工作位置预占 | 占位 → 执行 → 释放；后台在模型/工具批次边界保存结果并让位 | 真实事故 E3、E8。旧版本曾依赖错误会话名或 received 判断 | [agent_message.go:85](../picoclaw/pkg/agent/agent_message.go#L85)，[background_yield.go:23](../picoclaw/pkg/agent/background_yield.go#L23) |
| G2 持久 wake 租约 | 未 finished 且续租在 900 秒内时不领第二轮，模型执行期间每 15 秒 touch；静默闹钟的失联运行按 300 秒恢复 | 理论风险。防进程失联后永久占用或重复领取；未见现行超时值的自然事故样本 | [runtime_policy.py:356](../server-setup/runtime_policy.py#L356)，[agent_message.go:121](../picoclaw/pkg/agent/agent_message.go#L121)，[silent_alarms.py:153](../server-setup/silent_alarms.py#L153) |
| G3 发送去重与未确认状态 | 票据 reserved → sent/partial/rejected/unknown；同来源和指纹复用票据 | 理论风险。防重复发送和把未知送达当成功，现有精确异常分支以隔离测试为主 | [runtime_policy.py:459](../server-setup/runtime_policy.py#L459)，[runtime_policy.py:528](../server-setup/runtime_policy.py#L528) |
| G4 自动最终文本留内部、显式发送单独过闸门 | 自动内容 → 内部记录，发送工具 → 票据和真实回执 | 真实事故 E9，曾有未受管 cron 的内部结论外发。多入口的当前一致性仍需测试 | [output_isolation.go:16](../picoclaw/pkg/agent/output_isolation.go#L16)，[runtime_policy.py:459](../server-setup/runtime_policy.py#L459) |
| G5 内部静默结论阻断本轮后续发送 | 匹配后记 `internal_note_blocked:<source>` 及当前 claim.blocked，后续工具同样受阻 | 真实事故 E9 为来由，当前分条、caption 及改写后重发的阻断以隔离测试验证 | [internal_note_filter.py:56](../server-setup/internal_note_filter.py#L56)，[companion_cadence.py:138](../server-setup/companion_cadence.py#L138) |
| G7 轻量探测与心跳执行分开 | 未到期只探测，到期后由单循环和 executing 标记约束本进程执行 | 理论风险。防无到期任务仍唤醒模型，现有材料未给出 executing 这一层的独立自然事故 | [service.go:171](../picoclaw/pkg/heartbeat/service.go#L171)，[runtime_policy.py:580](../server-setup/runtime_policy.py#L580) |
| G6 写入失败不假报完成 | 短写撤回，Save 传播失败，已执行回执保留以便恢复 | 真实事故 E5 | [jsonl_append.go:20](../picoclaw/pkg/memory/jsonl_append.go#L20)，[jsonl_backend.go:19](../picoclaw/pkg/session/jsonl_backend.go#L19) |

## 普通聊天

使用路由得到的聊天会话。入站记录、原轮账本和模型视图分别承接接收事实、可回查内容和本次输入。

| 状态及位置 | 写入或读取规则 |
|---|---|
| runtime.sqlite `events(chat,msg,kind,content,created)`，会话接收/处理记录，`state.last_user/paused/unanswered` | inbound 按消息身份去重，先落地直接控制，再进入模型流程。最后处理状态由 TraceTurn 记录。[runtime_policy.py:257](../server-setup/runtime_policy.py#L257)，[agent_message.go:237](../picoclaw/pkg/agent/agent_message.go#L237) |
| turn-context.sqlite `context_turns.number/source_key/complete/deliveries`、`context_items`、`context_payloads`；原 JSONL 会话 | Sync 维护稳定轮号与完整消息，投影按已通过覆盖点读未覆盖原轮。[store.go:190](../picoclaw/pkg/contextledger/store.go#L190)，[context_fatfish.go:349](../picoclaw/pkg/agent/context_fatfish.go#L349) |
| Go 内存 `exec.visibleToolDefs`；工具 registry 的 schema TTL 与执行注册表 | 本轮保留已发现的 schema；按注册名称执行与说明是否展示分别处理。[pipeline_llm.go:43](../picoclaw/pkg/agent/pipeline_llm.go#L43)，[registry.go:249](../picoclaw/pkg/tools/registry.go#L249) |
| 模型输入的 `fatfish_turn_event`、暂时省略轮号；配置 context_manager、context_window、max_tokens | 每次模型调用重建来源块和预算视图；原文身份保留在存储。[fatfish_turn_event.go:16](../picoclaw/pkg/agent/fatfish_turn_event.go#L16)，[context_fatfish_budget.go:94](../picoclaw/pkg/agent/context_fatfish_budget.go#L94) |

转换主线为“接收并登记 → 控制状态先保存 → 路由和组装 → 模型/工具续接 → 实际发送与回执 → 标记本轮结束”。

| 规则 | 防的事故与分类 | 入口 |
|---|---|---|
| O1 收到撤销先落地，转发材料只记来源 | 真实事故 E3。防模型处理中断使停止要求没有写入，或旧回合恢复已关闭任务 | [runtime_policy.py:257](../server-setup/runtime_policy.py#L257)，[agent_loops.py:415](../server-setup/agent_loops.py#L415) |
| O2 固定原轮与真实来源、事件块区分真人和后台 | 真实事故 E2。历史内部回执曾混入真人轮，事件理解也出现偏差 | [store.go:190](../picoclaw/pkg/contextledger/store.go#L190)，[fatfish_turn_event.go:16](../picoclaw/pkg/agent/fatfish_turn_event.go#L16) |
| O3 新消息取消旧生成并保留续接来源 | 理论风险。防旧答复覆盖新消息，具体取消和续接场景由测试覆盖 | [turn_state.go:595](../picoclaw/pkg/agent/turn_state.go#L595)，[pipeline_llm.go:313](../picoclaw/pkg/agent/pipeline_llm.go#L313) |
| O4 schema 可见性与执行分开，本轮工具说明保留 | 真实事故 E4。两个规则分别防跨轮 TTL 到期不能执行、同轮说明中途消失 | [registry.go:249](../picoclaw/pkg/tools/registry.go#L249)，[pipeline_llm.go:43](../picoclaw/pkg/agent/pipeline_llm.go#L43) |
| O5 超输入预算只暂时省略完整旧视图 | 理论风险，明确协议边界。防截断当前工具链或把临时省略误写成已摘要覆盖 | [context_fatfish_budget.go:94](../picoclaw/pkg/agent/context_fatfish_budget.go#L94) |

## 陪聊

陪聊与自主活动共用 `agent:autonomy-<epoch>`，通过不同 wake.kind 和近期交流材料区分。

| 状态及存储位置 | 意义 |
|---|---|
| runtime.sqlite `state.next_chat_due`、`companion_interval_seconds`、`companion_due_explicit`、`companion_cancelled_source_id` | 下一次截止时间、成功发送后的间隔、显式时间优先以及本来源的明确取消。[companion_cadence.py:49](../server-setup/companion_cadence.py#L49) |
| `state.companion_source` JSON 的 version/source_id/source_message_id | 每次新真人输入推进来源版本，使先前候选失效。[companion_cadence.py:38](../server-setup/companion_cadence.py#L38) |
| `state.companion_current_claim` JSON 的 wake_id/epoch/version/source_id/claimed_at/blocked；`state.companion_send:<ticket>` JSON 的来源及 confirmed | 领取记录固定当轮身份，发送记录承接异步回执，避免迟到结果改变较新排程。[companion_cadence.py:91](../server-setup/companion_cadence.py#L91)，[companion_cadence.py:155](../server-setup/companion_cadence.py#L155) |
| 配置 companion_cadence_enabled；loop_autonomy.enabled/epoch；state.paused；wakes.finished/contacted | 共同决定是否可领、可发、可续排。[companion_cadence.py:45](../server-setup/companion_cadence.py#L45)，[companion_cadence.py:125](../server-setup/companion_cadence.py#L125) |

转换主线为“新消息撤销旧计划并换来源 → 真实发送回执或显式安排产生 due → 到期领取时清空 due → 发送成功续排，普通安静结束停止临时链”。

| 规则 | 防的事故与分类 | 入口 |
|---|---|---|
| C1 单一 due，到期先消费 | 理论风险。防一条临时链存在多个未消费计划；E6 证明正常链已运行，异常分支由隔离测试验证 | [companion_cadence.py:99](../server-setup/companion_cadence.py#L99) |
| C2 真人来源版本与自主 epoch 都核对 | 理论风险。前者防新消息到来后旧候选续排，后者防总开关撤销后的迟到回执复活。两者寿命不同 | [companion_cadence.py:125](../server-setup/companion_cadence.py#L125)，[companion_cadence.py:167](../server-setup/companion_cadence.py#L167) |
| C3 sent/partial 且有消息 ID 才按默认 60 秒续排，重复完成幂等 | 理论风险。防未知或失败送达产生假续排，E6 为正常真实发送依据 | [companion_cadence.py:167](../server-setup/companion_cadence.py#L167) |
| C4 普通静默结束清 due，显式时间保留，取消回执也不能自动恢复计划 | 理论风险，明确产品约定。防显式预约或取消被同轮自动逻辑覆盖 | [companion_cadence.py:188](../server-setup/companion_cadence.py#L188)，[companion_cadence.py:49](../server-setup/companion_cadence.py#L49) |
| C5 全天陪聊独立于自由活动窗口，最短 60 秒 | 理论风险，当前产品约定。防陪聊预约被自由活动时窗挪动 | [companion_cadence.py:49](../server-setup/companion_cadence.py#L49)，[companion_cadence.py:45](../server-setup/companion_cadence.py#L45) |
| C6 claim 带最近 24 条真实往来，另带同一份自主续记 | 理论风险为限定材料量的目的；续记的跨天失效已有真实事故 E7 | [companion_cadence.py:99](../server-setup/companion_cadence.py#L99) |

## 定时自由活动

runtime.sqlite 的 `loop_autonomy(enabled,epoch,interval_seconds,next_due,note)` 保存总开关、周期、下次机会和续记。`state.loop_autonomy_control` 保存 source_id、message_id、at 与控制结果，`loop_activity` 保存审计经过。配置 `autonomy_daily_window` 及其 extra_times 决定时段。

转换为“到期且在窗口 → 把 next_due 推到后续槽位 → 建立自主 wake → 显式发送或安静结束”。关闭会推进 epoch，并撤销同源的派生安排。活动续记通过 autonomy note 单独改写，当前读取时只拼接 note 文本。

| 规则 | 防的事故与分类 | 入口 |
|---|---|---|
| F1 总开关、默认间隔、下次时间分别管理 | 真实事故 E3。原先调整一次时间会同时改周期，关闭又曾变成长期延期；现已分开 | [agent_loops.py:351](../server-setup/agent_loops.py#L351) |
| F2 以 epoch 和较新控制 source_id 撤销旧轮及派生入口 | 真实事故 E3。防关主巡检后闹钟或迟到回执继续动作 | [agent_loops.py:396](../server-setup/agent_loops.py#L396)，[agent_loops.py:112](../server-setup/agent_loops.py#L112) |
| F3 安静一轮保留下一次自由机会，关闭后由新真人指令恢复 | 理论风险，明确产品约定。临时陪聊的安静停止与这里有不同结果 | [agent_loops.py:498](../server-setup/agent_loops.py#L498)，[agent_loops.py:396](../server-setup/agent_loops.py#L396) |
| F4 窗口与额外时点、300 秒领取窗口，错过槽位跳过 | 理论风险。防窗口外新唤醒及错过时段后的连续补醒；现有依据为时段需求与隔离测试 | [autonomy_window.py:34](../server-setup/autonomy_window.py#L34)，[autonomy_window.py:49](../server-setup/autonomy_window.py#L49) |
| F5 最近 12 条 user/assistant 与 note 进入自由活动材料 | 材料量限制为理论风险；note 缺时间和有效期的后果属于真实事故 E7 | [agent_loops.py:526](../server-setup/agent_loops.py#L526)，[agent_loops.py:388](../server-setup/agent_loops.py#L388) |

## 任务巡检与执行回执

任务目标的变化、外部执行的变化和接收回执的变化分开保存。DSH 和 Claude Code 复用 CodexBridge 的生命周期，使用各自前缀的表。

| 状态及位置 | 用途与入口 |
|---|---|
| runtime.sqlite `loop_tasks.version/monitor/report/next_due/origin_epoch`；data JSON 的 status/goal/completion/agreements/latest_instruction/instruction_version/control_version/monitor_cancel_source/deliverables/deliveries | row.version 随记录保存递增，instruction_version 随目标要求变化递增。monitor 管巡检，report 管外发，deliveries 保存成果校验。[agent_loops.py:171](../server-setup/agent_loops.py#L171)，[agent_loops.py:196](../server-setup/agent_loops.py#L196) |
| `loop_task_history`、`loop_task_events`、`loop_bindings(kind,key,task_id,epoch)`、`loop_receipts`、`loop_requests` | 分别记录旧版本、内部事件、来源绑定、外部回执快照和模型实际请求。[agent_loops.py:51](../server-setup/agent_loops.py#L51)，[agent_loops.py:538](../server-setup/agent_loops.py#L538) |
| `codex_jobs` 的 state/output/origin；`codex_chains` 的 state/receipt_state；`codex_events` 的 status/claim/claimed_until/processing_attempts/recovery/outcome | 外部执行结果与回执处理各有状态。事件 pending → claimed → processing，再到已处理、等待、关闭或恢复流程。[codex_bridge.py:469](../server-setup/codex_bridge.py#L469)，[codex_bridge.py:460](../server-setup/codex_bridge.py#L460) |
| Go `agent:task-<id>` 历史、实际工具回执、工作区 state/background-progress 检查点 | 巡检沿原任务接续，每次模型调用重新读当前任务和人格正本，真人到来时保存已完成结果。[fatfish_loops.go:42](../picoclaw/pkg/agent/fatfish_loops.go#L42)，[background_yield.go:29](../picoclaw/pkg/agent/background_yield.go#L29) |

转换主线为“登记目标 → 绑定 job/thread/turn → 到期巡检或执行端回执接续 → 更新进度或交付 → finish/交还”。回执处理失败转 recovery，只重做接收整理；监控停止保留已交出的外部执行。

| 规则 | 防的事故与分类 | 入口 |
|---|---|---|
| T1 每次模型调用读当前版本并保存实际请求 | 真实事故 E3。旧来源曾进入任务指令，更新只写文件不能证明执行端收到了要求 | [fatfish_loops.go:42](../picoclaw/pkg/agent/fatfish_loops.go#L42)，[fatfish_loops.go:99](../picoclaw/pkg/agent/fatfish_loops.go#L99) |
| T2 25 分钟主巡检与同任务 maintenance 复查合并 | 真实事故 E3。额外静默检查曾形成密集唤醒；领取维护检查会推后同任务主巡检 | [agent_loops.py:15](../server-setup/agent_loops.py#L15)，[silent_alarms.py:111](../server-setup/silent_alarms.py#L111) |
| T3 关监控取消派生检查，旧指令不能重开，独立额度单独保留 | 真实事故 E3。主巡检关闭后仍有派生唤醒和再派生 | [agent_loops.py:151](../server-setup/agent_loops.py#L151)，[agent_loops.py:129](../server-setup/agent_loops.py#L129)，[fatfish_loop_cron.go:11](../picoclaw/pkg/tools/fatfish_loop_cron.go#L11) |
| T4 派单按具体指令和 instruction_version 去重，终态证据优先 | 真实事故 E3。旧 queued 记录曾挡住新的工作；同一线程可承载不同任务 | [agent_loops.py:259](../server-setup/agent_loops.py#L259)，[execution_status.py:5](../server-setup/execution_status.py#L5) |
| T5 回执 claim/start/ack、处理租约与 recovery | 回执失败后重新派执行的防护为理论风险，E5 的真实存储故障提供恢复场景；精确重复派单分支已有隔离测试 | [codex_bridge.py:469](../server-setup/codex_bridge.py#L469)，[fatfish_codex.go:69](../picoclaw/pkg/agent/fatfish_codex.go#L69) |
| T6 report 总开关与单任务开关只控制后台联系，真人新消息可回应 | 理论风险，明确产品约定。防停止汇报被误当停止任务，或后台设置堵住真人新要求 | [agent_loops.py:571](../server-setup/agent_loops.py#L571)，[agent_loops.py:538](../server-setup/agent_loops.py#L538) |
| T7 finish 前核对 deliverables 的 verified 回执 | 真实事故 E3 的交付边界，后续已有真实文件校验交付；防执行完成文本冒充文件已到位 | [agent_loops.py:161](../server-setup/agent_loops.py#L161)，[task_artifacts.py:12](../server-setup/task_artifacts.py#L12) |

## 额度守候

新路径使用 `loop_quota(provider,reset_at,status,task_ids)` 和 `loop_quota_watch(provider,enabled,origin_epoch)`，真实额度来自各后端 `*_usage_cache(payload,created)`。旧路径仍可使用 `silent_alarms.purpose=quota_watch/quota_reset`、`silent_alarm_runs` 及 `loop_bindings`。两条路径都会路由到有效任务或 `agent:quota`。

新路径为“真实回执给 resets_at → 按 provider/reset_at 复用 pending → 到期领取记 checked → 查额度并续接有效任务”。旧周期闹钟是 pending → running → 下次 pending；旧一次性 reset 出错会记 error，避免变成新的五分钟重试链。

| 规则 | 防的事故与分类 | 入口 |
|---|---|---|
| Q1 reset_at 必须来自缓存的真实限额窗口，按 provider/reset_at 合并 | 理论风险。防猜测重置时刻和同窗口重复监听；11 月 5 日确有旧额度闹钟运行，但不能据此认定新合并规则曾失效 | [agent_loops.py:468](../server-setup/agent_loops.py#L468)，[agent_loops.py:489](../server-setup/agent_loops.py#L489) |
| Q2 任务收口与独立额度监听分开，来源 epoch 仍生效 | 理论风险，明确产品边界。防一个任务结束把独立监听一起关掉，或已关闭自主来源被重新唤醒 | [agent_loops.py:144](../server-setup/agent_loops.py#L144)，[agent_loops.py:583](../server-setup/agent_loops.py#L583) |
| Q3 旧 purpose 额度轮有快照、合并与一次性错误处理 | 存在真实旧路径的记录 E3；按 purpose 合并和错误终态的具体保护以测试为主，分类为理论风险 | [pulse_policy.py:34](../server-setup/pulse_policy.py#L34)，[silent_alarms.py:120](../server-setup/silent_alarms.py#L120) |

## 摘要整理及约定退场

这条链同时处理候选、有效摘要和独立约定，三个对象不能用同一个 accepted 布尔值概括。

| 状态及位置 | 规则与用途 |
|---|---|
| turn-context.sqlite `context_sessions.active_version/covered/revision/cooldown/legacy_summary` | 有效版本、覆盖点、来源快照版本、失败冷却和旧策略参考。[store.go:128](../picoclaw/pkg/contextledger/store.go#L128) |
| `context_summaries.id/parent/cover_from/cover_to/status/reason/raw/body/agreements/response` | 每次尝试留候选，通过时在同一事务推进会话版本与覆盖点。[summary.go:442](../picoclaw/pkg/contextledger/summary.go#L442) |
| `context_agreements.data` JSON 的 id/text/state/sources/changed_by/home/home_quote/terminal_since/archived_version | 当前约定事实与摘要候选独立。正文失败时仍可能保存有新证据的约定变化，只有通过版本推进退场标记。[summary.go:18](../picoclaw/pkg/contextledger/summary.go#L18)，[summary.go:442](../picoclaw/pkg/contextledger/summary.go#L442) |
| Go 内存 running、Material.Previous/MaxNumber/Past/Recent/Homes；配置 keep_recent/cooldown_seconds/sample_requests | 同时最多一份后台摘要，生成前保存本批来源快照，最近完整回合受保护。[context_fatfish_summary.go:21](../picoclaw/pkg/agent/context_fatfish_summary.go#L21)，[context_fatfish_summary.go:68](../picoclaw/pkg/agent/context_fatfish_summary.go#L68) |
| 上下文 budget、70% 启动水位、55% 目标、1800 字、60 秒两次生成期限、4096 输出、600 秒默认冷却 | 来源为实现默认值和预算计算，分别约束聊天输入、摘要材料、正文长度和重试费用。[context_fatfish_summary.go:68](../picoclaw/pkg/agent/context_fatfish_summary.go#L68)，[context_fatfish_summary.go:217](../picoclaw/pkg/agent/context_fatfish_summary.go#L217) |

转换为“到水位且不冷却 → 形成过往/近期快照 → 生成并 Validate → Sync/Rebase → 记录 rejected 或 accepted → 通过后激活并移出原文前缀”。允许的一次定向修正共用本轮期限。约定终态先展示并写 terminal_since，下一版通过写 archived_version；State 跳过归档项。

| 规则 | 防的事故与分类 | 入口 |
|---|---|---|
| S1 候选留底，版本与覆盖点事务激活 | 理论风险。防拒绝候选或半写入替换旧摘要，既有真实错引/超长拒绝 E1 证明拒绝路径在用 | [summary.go:442](../picoclaw/pkg/contextledger/summary.go#L442) |
| S2 来源、说话人、覆盖范围、长度校验；home 失效降为正文 | 真实事故 E1。三规则死锁已修复，逐字 Quote 的严格来源检查仍在 | [summary.go:240](../picoclaw/pkg/contextledger/summary.go#L240)，[summary.go:410](../picoclaw/pkg/contextledger/summary.go#L410) |
| S3 漏写已有约定由程序补，正文不通过仍可独立保存有依据的新变化 | 真实事故 E1 为保留约定的来由；分开保存可防正文失败掩盖刚确认的状态变化 | [summary.go:330](../picoclaw/pkg/contextledger/summary.go#L330)，[summary.go:474](../picoclaw/pkg/contextledger/summary.go#L474) |
| S4 Rebase 只接受本批过往、版本、覆盖和最新轮号未变的 revision 更新 | 真实事故 E1。最近回复送达造成 revision 增长，旧代码因此从未进入修正；真实来源变化仍应拒绝 | [summary.go:354](../picoclaw/pkg/contextledger/summary.go#L354)，[context_fatfish_summary.go:197](../picoclaw/pkg/agent/context_fatfish_summary.go#L197) |
| S5 一次修正、60 秒共用期限、失败冷却 | 真实超长及修正被跳过见 E1。限制次数和冷却数值的具体事故来由未见，属于理论费用与负载风险 | [context_fatfish_summary.go:217](../picoclaw/pkg/agent/context_fatfish_summary.go#L217)，[summary.go:108](../picoclaw/pkg/contextledger/summary.go#L108) |
| S6 保护最近 6 个完整回合；摘要未通过时临时省略不推进 covered | 理论风险。防近期消息被整理过早或临时预算裁剪丢失覆盖依据 | [context_fatfish_summary.go:93](../picoclaw/pkg/agent/context_fatfish_summary.go#L93)，[context_fatfish_budget.go:94](../picoclaw/pkg/agent/context_fatfish_budget.go#L94) |
| S7 终态在通过版本展示一次后归档，active 清计时，拒绝不推进 | 理论风险，明确产品方案 E10。防终态记录长期占预算，也防未展示就退出 | [summary.go:540](../picoclaw/pkg/contextledger/summary.go#L540)，[store.go:459](../picoclaw/pkg/contextledger/store.go#L459) |

## 重复、冲突与无效规则

以下区分当前可直接查证的重复、已经修复的历史冲突，以及仍需具体场景证明的推断。

| 发现 | 证据与场景 | 判断 |
|---|---|---|
| D1 同一批已有约定在摘要材料中出现三份 | [material.go:103](../picoclaw/pkg/contextledger/material.go#L103) 将约定放入 confirmed_agreements、candidate_template.agreements，version>0 时 previous_accepted.agreements 又带一次。程序已自动补回旧约定 | 静态确认的重复材料。增加输入和模型抄回负担，可以先精简请求格式；数据库里的历史快照与当前约定各有用途 |
| D2 三个修正原因已没有当前 Validate 的拒绝出口 | [summary.go:108](../picoclaw/pkg/contextledger/summary.go#L108) 仍处理 unmatched_prose_quote、unverified_agreement_home、agreement_omitted。现在对应内容分别去引号、归一 home、程序补约定 | 在当前自动生成场景不可达。历史 reason 仍可保留作诊断，工具链不必继续维持这些生产修正分支 |
| D3 background_allowed 健康时恒为 true | [runtime_policy.py:637](../server-setup/runtime_policy.py#L637) 从导入提交即为固定放行。[provider.go:493](../picoclaw/pkg/providers/openai_compat/provider.go#L493) 每个带 background 标记的模型请求仍调用；[mcp_tool.go:260](../picoclaw/pkg/tools/integration/mcp_tool.go#L260) 的同名调用只对旧 heartbeat session 触发 | 可达但无业务判断，额外增加一次 RPC 和最多 8 秒失败等待。没有找到这一空壳必须保留的事故来由；真正状态检查在 loop_context 和发送入口 |
| D4 同一后台输出事实在多处重建 | [pipeline_finalize.go:46](../picoclaw/pkg/agent/pipeline_finalize.go#L46)、[agent_outbound.go:60](../picoclaw/pkg/agent/agent_outbound.go#L60)、[output_isolation.go:16](../picoclaw/pkg/agent/output_isolation.go#L16)、[runtime_policy.py:470](../server-setup/runtime_policy.py#L470) 分别组合来源、前缀和发送标记 | 重复判断可证实，当前新事故未证实。历史 E9 说明最终渠道闸门有实际价值，适合收拢判定来源并继续逐个发送入口验证 |
| D5 quota_wait 与旧 silent_alarm 的 quota purpose 管同一重置机会 | [agent_loops.py:468](../server-setup/agent_loops.py#L468) 与 [runtime_policy.py:437](../server-setup/runtime_policy.py#L437) 都路由到 quota/task，但去重键分别为 provider/reset_at 和 chat/purpose | 静态确认的并行机制，跨机制没有同一个去重键。E3 证明旧路径历史上真的在用，现有线上是否还残留待执行项需要后续实施前核对 |
| D6 续记缺时间边界与下一轮独立判断相冲突 | [agent_loops.py:388](../server-setup/agent_loops.py#L388) 只覆盖 note 字符串，写入时间留在另表；两种自主领取都原样带 note。代码不提供写入时间或结构化有效期，模型需从正文自行推断 | 真实事故 E7，当前结构仍保留该形态。总开关 epoch 不表达一段短期续记的有效期 |
| D7 旧节拍 ended_without_share 钩子恒为 false | [pulse_policy.py:48](../server-setup/pulse_policy.py#L48) 与 [silent_alarms.py:133](../server-setup/silent_alarms.py#L133) | 当前取消分支没有实际作用，查不到现存事故来由。可随旧节拍收尾顺手删除，单独做它的收益很小 |
| D8 control_version 没有程序侧比较者 | [agent_loops.py:153](../server-setup/agent_loops.py#L153) 只写入 JSON，运行判定实际看 monitor/status/monitor_cancel_source、row.version 和 instruction_version | 它目前是记录字段，不能声称靠它阻止旧控制。可澄清字段含义，是否删除取决于审计记录是否仍需要，不列为主要问题 |
| D9 旧 context manager 与旧 ordinary wake 的适用场景不同 | [context_legacy.go:47](../picoclaw/pkg/agent/context_legacy.go#L47) 的 Fatfish 分支只在选用该 manager 时走；[runtime_policy.py:386](../server-setup/runtime_policy.py#L386) 返回后不会进入后面的 ordinary/exploration 分支 | 在特定配置场景不触发，配置切换后仍可能触发。当前只有历史配置样本，不能把这些分支整体宣称为全局死代码 |
| D10 陪聊优先领取与其他到期工作共用同一 wake 执行机会 | [runtime_policy.py:371](../server-setup/runtime_policy.py#L371) 先于 diary、loops.claim，后者内部又按 quota、task、autonomy 排序 | 静态优先级已确认。在发送后本轮继续很久、陪聊截止时间又已到的场景可能延迟其他到期任务，尚未查到自然饥饿事故。先写场景测试，再决定是否需要改变优先级 |

历史三规则死锁已经修复。当前最相近的遗留形态是 D6，旧续记仍作为每轮输入、却没有能直接判断失效的元数据。D1/D2 属于修复后没有一起收尾的材料与分支，D4/D5 属于同一业务事实在多个入口维持。

## 前五项精简建议

按可见收益、回归风险和工作量综合排序。工作量是代码范围的相对判断，尚未估算工期。

| 顺序 | 方案 | 收益 | 风险 | 工作量 |
|---|---|---|---|---|
| 1 | 摘要材料只保留一份约定依据，清理失效修正分支 | 高，降低重复输入和模型抄回负担 | 低到中，请求格式和提示词需一起验证 | 小 |
| 2 | 给活动续记补写入时间和适用边界 | 高，处理已有跨天错误 | 中，需区分短期决定与长期兴趣 | 小到中 |
| 3 | 移除恒放行的 background_allowed 调用 | 中，减少可达 RPC 及失败点 | 低到中，需确认运行时不可用时的处理要求 | 小 |
| 4 | 收拢后台输出分类与自动发布判断 | 高，减少多入口语义漂移 | 中，多种渠道和回调都需覆盖 | 中 |
| 5 | 额度守候收敛到 provider/reset_at 排程 | 中，减少两套状态和去重范围 | 中到高，已有闹钟与任务归属需一次迁移 | 中 |

### 1. 摘要约定材料与修正分支

合并 D1 的三份完整约定为一个明确来源，候选模板保留结构即可，已有约定由程序承接。提示词说明只需提交新增或有依据的变化，保留旧候选全量写法的读取兼容。删除 D2 中当前校验已不再产生的三个自动修正分支及专门依赖它们的旧测试。有效的超长、截断、JSON 不完整和结构化 Quote 错误继续走一次修正。

行为变化是模型少抄已有约定，验收后的最终约定集合保持现有规则。验证要覆盖无变化省略、active 变化必须有新来源、正文被拒仍保存已核对变化、近期送达 Rebase、1800 字边界和终态展示/归档。用同一批离线候选对照接受结果，再检查新材料只携带一份约定依据。

### 2. 活动续记的时间与适用边界

把当前 note 的文本、写入时间和可选失效时间放在同一条可读记录里，领取时一并带入。短期行动决定明确适用到何时，长期兴趣或待续主题可以没有硬截止。由模型写明确边界，程序按结构化时间判断有效性，避免再加一套猜测自然语言时长的正则规则。旧纯文本先按已有 loop_activity 核对写入时间，核对不到时标明时间待确认，再整理为带来源的新续记。

行为变化是过期的短期决定退出当前行动依据，相关历史仍可回查。验证跨日、精确截止、没有截止的长期主题、显式陪聊预约、真人新消息更新意愿和自主总开关。先用 E7 的日期结构做脱敏离线回放，确认新旧时间不会混在一起。

### 3. 恒放行 RPC

删除 provider 和旧 MCP heartbeat 路径上的 background_allowed 空壳调用。模型前的 loop_context、逐工具检查和实际发送票据继续各自负责当前任务、撤销及投递。若需要统一定义运行时不可用时怎样结束当前轮，把该要求写在这些已有的有效检查上。

健康情况下减少一次模型前 RPC；故障情况下去掉由空壳独立制造的拒绝点。验证 task、autonomy、quota、旧 heartbeat、日记和 cron 的模型调用，分别覆盖运行时正常、失败、来源已撤销和真人接管。检查移除后实际有效闸门仍被调用。D7 可在同一维护批次清理，其收益主要是少一个误导分支。

### 4. 输出分类入口

把 Go 侧分散组合 session 前缀、background 标记、回执来源和是否显式发送的逻辑收进统一判定，自动发布、流式输出和内部保存都读取同一结果。运行时的最终发送入口继续核对当前撤销状态和真实票据。内部静默文本检测保留在实际发送处，来源、分条正文和 caption 使用同一判定输入。

行为目标是同一来源在各入口得到一致结果。验证普通真人回复、任务与自主 final、quota、独立 cron、异步查询、旧回调后新真人 steering、文字/贴纸/图片及 partial/unknown 回执，特别检查内部内容没有进入已发送聊天历史。先收拢纯分类，再处理各渠道接入，避免同时改发送记账。

### 5. 单一额度排程

新额度守候统一进入 loop_quota 和 loop_quota_watch，以 provider/reset_at 为去重依据。旧 quota_watch/quota_reset 闹钟先列出待执行项，核对已有真实 reset 证据后做一次映射，归属、独立监听和已取消状态都保留，旧历史可读。普通 reminder、任务 maintenance 及自由活动继续承接各自目的。

行为变化是两种入口共享同一个重置机会。验证相同与不同 provider 的同一时间窗口、一个窗口多个任务、任务完成仍监听、旧 epoch 撤销、处理失败、重复调用以及旧闹钟迁移后只有一个可领取机会。迁移范围必须先有实际待执行记录清单，再安排实现和验收。

## 建议保持的边界

- **保留来源版本和自主 epoch。** 新真人输入只改变当前交流来源，总开关改变整批自主来源的有效性。E3 和 E6 说明它们不能互相替代。
- **保留 row.version 与 instruction_version 的不同用途。** 进度更新会改变记录版本，具体要求未变时仍应复用同一执行请求。control_version 可澄清为记录用途，删除它的收益较小。
- **保留执行完成、回执处理和文件交付的状态。** E3、E5 证明实际执行成功后，接收和存储仍可能失败。把它们压成 done 会失去恢复依据。
- **保留本地执行位置与持久租约。** 前者约束当前进程里的并发，后者处理进程失联后的恢复。可以集中记录常量与含义，当前证据不足以支持直接删其中一层。
- **保留摘要的事务激活和 Rebase 边界。** E1 的问题已经靠比较真正相关的来源范围解决，覆盖点、过往原文或有效版本变化仍应拒绝旧候选。

## 验证与交付

本次核对了关键表结构、状态读写者、自动生成的错误出口和对应历史记录，并在隔离 Git 副本运行现有陪聊、任务循环、运行恢复和旧节拍四组 Python 用例，共 82 项全部通过。详细命令和退出码保存在仓库同级 `server-setup/state/state-audit-20261115/existing-tests.json`。上述建议中的新增回归场景属于后续实施验收要求。

源码行号按本文固定提交定位，后续代码变化需要重新核对。旧版本发布文档中的间隔、窗口、严格引号和修正范围描述用于解释历史，当前行为以本次代码逐项检查为准。
