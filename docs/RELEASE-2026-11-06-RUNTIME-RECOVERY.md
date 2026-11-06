# 2026-11-06：任务撤销、回执恢复和文件交付

修复 11 月 5 日实际发生的磁盘写满、旧请求复用及后台撤销不彻底问题。沿用现有 Go 主循环、SQLite 队列和 Windows 连接器，没有引入新的调度系统。上线时按用户要求暂未推送 GitHub；随后用户授权提交并推送，README 同步补充了循环隔离、真人让位及任务补充要求的两步同步机制。

## 行为与实现

| 问题 | 最终行为 | 主要实现 |
|---|---|---|
| 写盘失败后丢失回执 | 执行终态保留，回执处理独立重试；恢复回合禁止再次向执行端派单，允许收取已有文件 | `server-setup/codex_bridge.py`、`execution_status.py`，Go `fatfish_codex.go`、`pipeline_execute.go` |
| 旧排队请求挡住新指令 | 去重核对具体指令与要求版本；已核销请求不会被轻量读取重新标成不确定。新目标独立建任务、执行链，可以继续使用原生聊天 | `agent_loops.py`、`codex_bridge.py` |
| 状态口径混杂 | 请求接收、执行、回执处理、交付分别保存；完成证据优先于历史排队字段 | `execution_status.py`、链表新增状态列 |
| 查询慢与检查叠加 | 默认只读会话元数据和原生队列，显式 `detail=history` 展开；前台队列先于后台观察，同任务维护检查合并 | `codex-control/worker.py`、`silent_alarms.py` |
| 已说完成却没有报告 | `artifact` 原样收取执行端文件，工作区 CLI 原子落盘、读回并核验字节数与 SHA-256；文件字节另存，模型只看到元数据 | `task_artifacts.py`、`workspace-tools/task_artifact.py` |
| 关了后台仍再醒 | 真人撤销先写控制版本；取消派生排程，运行中在安全边界让位，迟到结果只保存。较早回合不能重新开启；独立额度与普通提醒保留 | `agent_loops.py`、`silent_alarms.py`、Go `fatfish_loop_cron.go` |
| 自由时钟改成极长间隔代替关闭 | 下一次时间、循环间隔、总开关三个操作分开；关闭推进 epoch，旧 epoch 不再准入 | `agent_loops.py`、`runtime_policy.py` |
| 工具能力在同轮消失、后台占住真人 | 同轮保留已发现的 schema；任务与自主活动会话同样在模型、工具边界响应真人优先 | Go `registry.go`、`pipeline_llm.go`、`background_yield.go`、`turn_coord.go` |
| 磁盘满出现 EOF、JSONL 损坏 | 本地存储故障返回结构化 503；追加短写或同步失败撤回本次追加并向上报告。历史恢复保留损坏原件，以已记录来源恢复消息 | `budget-gateway.py`、`session_recovery.py`，Go memory/session 模块 |

另外将 `picoclaw/.gitignore` 中的 `onboard` 锚定为根目录产物 `/onboard`，纳入原本被误忽略的四个 onboarding 源码文件，使当前版本可重建。未更改模型、上下文窗口或人格正本。保留其他工作者的未提交修改；其中已有的查询缓存改动仍保持原先的部署边界，未夹带上线。

## 验证

- 新恢复测试 23 项通过：撤销与旧来源复活、精确去重、独立执行链、版本更新、完成终态、回执恢复不重新派单、不重复发送、维护合并、额度保留、文件交付和真实损坏格式。
- 任务循环 19 项、协作收口 2 项、节拍策略 16 项、工具效率 17 项、SSH 入口 8 项通过；保留并行改动合回主工作区后，恢复 23 项、桌面 93 项和工具效率 19 项再次通过。网关基础 8 项、缓存 7 项、并发 1 项、实际 HTTP 存储故障 1 项通过。
- 桌面连接器 93 项通过；Go 相关包 241 项通过，Linux 二进制交叉编译成功。
- 老的 `test-codex-bridge.py` 在原始 HEAD 和本次候选均有相同的 2 失败、9 错误：旧测试按十列插入已扩展的表，另有过期离线权限假设。本次没有宣称这个历史测试套件全绿；现行协议由上述恢复与收口测试覆盖。

可在仓库根目录复验新增检查：

```sh
python3 server-setup/test-runtime-recovery.py
python3 server-setup/test-gateway-storage-recovery.py
python3 server-setup/test-agent-loops.py
python3 server-setup/test-collaboration-finish.py
python3 server-setup/test-codex-ssh-entry.py
python3 -m unittest discover -s codex-control -p 'test_*.py'
```

## 实际上线回执

- 三个服务已启动，`/health` 为 `ok`，`/ready` 为 `ready`。最后一轮主程序切换到就绪用时 **4.529 秒**；随后仅重启记忆工具服务完成读状态修正，用时 **1.915 秒**。部署前的两次保护性回滚亦留档。
- 最终 Linux 二进制 SHA-256：`cc0b89ac92c75a7a1a5f1fee77ee8ab012ba565f25f5465de82666f71f6f9ead`。十个线上模块逐一与最终清单匹配。
- 两份损坏会话已修复，共 **1676 条** JSONL 记录全部可解析。普通聊天恢复使用原始用户事件，截断的附加表达经验保存在损坏原件归档中，没有重建或编造这段动态提示；后续消息保留。SQLite `quick_check=ok`。
- 旧排队请求已核销为 `superseded / not_started`；后来的真实回合仍为 `completed / succeeded`。任务执行链独立，回执为 `closed_after_handoff`，交付为 `verified`。
- 自主活动为 `enabled=0 / epoch=2`。已交还任务 `monitor=0`；关联待执行派生闹钟数量 **0**。实际 `loop_context` 对旧任务唤醒和旧自主 epoch 都返回 `active=false`。两条原有独立提醒继续有效，没有停止执行端任务。
- 真实收取既有 Grok 报告，**24518 字节**，写入当前工作区 `state/grok-bot-survey-20261105.md`。执行端与服务器读回 SHA-256 均为 `347cb0befd674c3f9309f92daa24b9dc031ce1b1812fa4bf9e89d6adef8a818e`。没有新增研究回合。
- 三次真实只读验收：文件收取 **3.070 秒**，轻量状态读取 **4.618 秒 / 0.998 秒**（包含队列等待，样本较小）。回调通知关闭，没有发送 Telegram 测试消息。新版本期间三个服务无新增 error 级日志。
- Windows 只重载 Codex 连接器；原生路由文件哈希不变，其他编码连接器保留。

完整私有回执位于部署维护目录 `server-setup/state/runtime-repair-20261105/`，不纳入公开仓库。文件包括 `deploy-manifest.json`、`verification.json`、`live-acceptance-final.json`、`worker-deployment.json` 和测试日志。

## 回滚

服务器备份入口：`/var/backups/picoclaw/runtime-repair-20261105T151217Z/`，含原始源码、二进制、配置、人格、日记、数据库和损坏会话原件。

```sh
sudo python3 /var/backups/picoclaw/runtime-repair-20261105T151217Z/rollback.py
```

本地维护目录的 `ROLLBACK.ps1` 顺序回滚服务器代码和 Codex 连接器。回滚检查当前哈希，遇到后续修改会停止，防止覆盖其他工作。默认只回滚代码，保留修复后的历史、真实交付记录及之后的新消息，也保留用户撤销状态。

已交执行端的自主任务继续运行这一边界沿用用户确认；本次无新增未定项。
