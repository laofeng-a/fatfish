# 日记、摘要与两类唤醒交付（2026-11-05）

已上线。沿用原有 Go Agent、Python Runtime、SQLite、heartbeat 租约、cron 和执行端队列，没有另建排程或记忆数据库。上下文窗口仍为 65536，max_tokens 仍为 8192。

## 行为与提示词

| 范围 | 原来 | 现在 |
| --- | --- | --- |
| 聊天摘要 | 容易围绕任务进度、执行结果展开；有 1500 字倾向 | 以双方回应、相处、接梗、感受、选择和澄清经过组织；技术执行信息归任务记录。正文加独立约定仍硬限 1800 字符，自然长短 |
| 摘要材料 | 工具简述、内部思考容易混进整理输入 | 模型专用投影去掉 recorded_thoughts 与执行简述；读回真实聊天、日记的工具内容标为 read_sources。原始记录不删除 |
| 日记 | 写入 6000 字节，通用读取 8000 字节 | 日记写入、读取和搜索读取统一 18000 字节；MEMORY、人格原有限额保持不变 |
| 日记注入 | 容易误解为每轮带正文 | 仍只列今天、昨天、前天已存在的路径，memory_find 找片段、memory_note_read 展开正文；早期日记继续可检索 |
| 第一类任务 | 分散巡检、回执缺少稳定目标 | task_loop 统一登记与派单；持久化目标原话、消息来源、版本、有效约定、执行端完整 ID、进展及遗留，25 分钟巡检 |
| 额度守候 | 与具体巡检耦合 | quota_wait 使用执行端真实重置时间，同一窗口复用；任务收口不关闭独立额度监听，已有待执行请求复用 |
| 汇报 | 自动轮次最终文本可能外发 | 自动最终文本留内部；汇报总开关和任务开关默认开，联系 jg 显式发送；关汇报仍可推进任务 |
| 第二类活动 | 安静一轮可能失去后续机会 | autonomy 默认 900 秒、可调，最低 120 秒；安静结束仍保留下一次。少量续记按需保存 |
| jg 撤销 | 提示词要求不足以撤销派生入口 | 来源 epoch 标记派生闹钟、cron、队列和回调；关闭后逐次模型调用及逐个工具边界拦截新行动，迟到回执仅存档，只有 jg 可恢复 |

任务上下文逐次从同一份 SOUL、core、AGENT 正本和 OPERATIONS 的任务操作段读取，不维护第二套执行规则；不自动带入普通聊天摘要和陪聊历史。每次实际模型请求重新装入最新任务版本，记录最终组装请求以核对 ID、版本和内容。自主活动保留正常相处背景和近期真实往来。

明确约好继续且仍有效的话题写成自然接话线索；没有增加 pending_topics。早期经历保留日期和主题，细节查日记，引文查原轮。原有候选版本、来源校验、约定保护、验收后激活和推进覆盖点、失败保留旧版及原文的机制继续保留。

## 文件范围

- Python：新增 `agent_loops.py`；修改 `runtime_policy.py`、`codex_bridge.py`、`silent_alarms.py`、`pulse_policy.py`、`reminders.py`，承接隔离任务、额度、撤销与回执路由。新增迁移、回滚脚本及运行测试。
- 日记与摘要：`memory_notes.py`、`diary_maintenance.py`、`fatfish_summary_prompt.txt`、`contextledger/material.go`、`agent/context.go` 及相应测试。
- Go 接入：新增 `agent/fatfish_loops.go`、`tools/fatfish_loop_cron.go`；修改模型调用、工具执行、消息接入、回执、输出隔离及 runtime 协议。`turn_coord.go` 移交 steering 后清空原队列，避免同条补充被再注入。
- `memory-reader.py` 仅将内部 `/runtime/loop_context` 请求大小上限改为 2 MiB，以容纳实际组装请求；其他入口仍为 65536 字节。部署保留服务器现有来源配置，不能直接用仓库占位配置覆盖。
- 模板：AGENT、HEARTBEAT、OPERATIONS。线上 AGENT 通过 revision 写入：2641 → 3944 字节；HEARTBEAT：3959 → 2633 字节。SOUL、USER、core、MEMORY 未改。
- 已上线的并行历史视图修复保留：`context_fatfish.go`、`fatfish_history_view.go` 和测试，回执位于 `state/history-style-fix-20261105/HANDOFF.md`。其余现存并行 Python、worker 改动不纳入本次提交。

## 验证与真实回执

| 验证 | 结果 |
| --- | --- |
| Python 任务循环 | 19 项通过：隔离上下文、版本、逐工具撤销、迟到回调、任务/汇报开关、静默续醒、额度独立、窗口合并、待执行请求复用、重复发送保护等 |
| 日记容量 | 12 项通过：恰好 18000 字节可写、读回、搜索；超限拒绝；其他文件限制保留 |
| 原有策略 | pulse 16 项通过；与线上实现匹配的 tool-efficiency 17 项通过。工作区另一条工具效率改动保留，未拿其未上线断言替代生产验证 |
| Go | Linux 针对性验收 40 个测试及子项通过，2 个需私有材料的默认测试跳过；私有真实摘要回放另外执行通过。Windows 另验证回执任务路由、cron、contextledger 及同轮最新目标注入 |
| 真实摘要 | 私有数据库副本：第 481–521 轮，验收激活 version 3，渲染后 656 字符；保留感受、误会和澄清。第一次候选错引被拒绝，旧版、原文及覆盖点保留；修正提示后通过。未强行推进线上摘要覆盖点 |
| 真实日记材料 | 11 月 4 日真实往来样本 6977 字节，隔离副本写入、读回、检索通过，超限拒绝，旧版本保留；没有把验收样本写入线上日记 |
| 实际任务请求 | 线上捕获 8 次组装请求，73445–121221 字节；同一任务 version 2 → 3，任务 ID、版本、正本 AGENT、同源操作规范均在实际请求中。当前 readback 为 version 5，含最新撤销消息 5601 |
| 真实自主活动 | 一轮自动活动最终文本留内部，确认的 policy 外发为 0；当时总开关开启且下次机会仍在。后续她已自行调整间隔，未强制重设为 15 分钟 |

旧版 memory-notes 全套中有 3 项既有授权/来源断言失败，改前改后相同，记录在 `preexisting-note-assertions.json`；本次没有通过收紧现有授权来“修绿”。不能将该旧全套称为全绿。

迁移初期曾误恢复一个旧任务巡检；核对到 jg 原话“把巡检关了吧”（消息 5601）后已关闭，并加入取消锁和迁移保护。该轮确认的 policy 外发为 0。目前两个旧任务 monitor 均为 false；执行端队列保留。不是为了验收重新开启旧巡检。

部署时旧日记哈希完整核对。随后今天日记由正常活动更新，旧内容完整保留在 note_versions 158；更早日记未变化。没有二次语义压缩、正文迁库或新增多级索引。

可复用验收命令（独立临时目录；不触发 Telegram 发送）：

```sh
cd /var/tmp/fatfish-agent-loops-diary-20261105
python3 test-agent-loops.py
python3 test-pulse-policy.py
python3 test-tool-efficiency.py
curl -fsS http://127.0.0.1:18790/health
curl -fsS http://127.0.0.1:18790/ready
```

日记边界、原断言对照和私有真实回放已经留下日志，达标后不反复对线上会话重做验收。

## 部署与回滚

实际运行路径 `/usr/local/bin/picoclaw`。本次 Go 版本 SHA256 `17abf842cb8718752c8d34b65c530cc91feeced5fd1cd57a9729262f14094a12`；后续历史视图修复以其为可复现基线，当前运行 SHA256 `93e687f9c98bfaefdcd900b0756389dcec8677d6d4574add4b872f4dc8cb8040`，保留本次功能。

最后核对：主进程 PID 2972690，记忆服务 2977867，预算网关 2874440；health/ready 均正常，预算网关未因本次工作重启。新增 task_loop/autonomy/quota_wait 已实际注册，10 个 Python 模块哈希匹配部署记录。

首次切换误用了仓库来源占位配置，造成主服务不可用 87.634 秒；已恢复服务器原配置，仅保留内部请求上限改动。后续只在无活动 wake 时重载记忆服务，最后一次 1.034 秒。此为实际中断记录，不宣称无中断上线。

服务器备份 `/var/backups/picoclaw/agent-loops-diary-20261105/` 保存原模块、人格、日记、数据库和原二进制；本地私有证据包位于仓库外 `server-setup/state/agent-loops-diary-20261105/evidence/`，不提交真实聊天、配置或凭据。

当前主程序包含后续修复，完整回滚需按部署逆序执行：

```sh
sudo python3 /var/backups/picoclaw/history-style-fix-20261105/rollback.py
sudo python3 /var/backups/picoclaw/agent-loops-diary-20261105/rollback.py --backup /var/backups/picoclaw/agent-loops-diary-20261105
```

脚本已语法校验，未执行回滚。先比对模块哈希、二进制及 profile revision，遇到后续修改即拒绝覆盖；不恢复旧数据库来覆盖上线后聊天、日记和任务记录。旧自动安排仅在迁移状态仍匹配时恢复。

原未定边界已由 jg 确认：关闭第二类时，已经交给 Codex／DSH／Claude Code 的任务保留执行端当前运行；不派新动作、不因迟到结果恢复活动或外发。无剩余待定项。
