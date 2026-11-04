# 协作收口与上下文整理

同日后续的摘要来源修复及正常水位真实回放结果，见 [摘要来源补充修复](RELEASE-2026-11-05-SUMMARY-SOURCES.md)。

2026-11-05（UTC+08）部署。依据 `HANDOFF-CONTEXT-REFORM-20261104.md`，未切换模型、Seahorse 或向量服务。

## 协作任务

源码提交 `1cdfb79`：在当前 AGENT 正本和操作指南中小范围合入自主推进、合理取舍、复用有效证据、达标收口的原则。Codex、DSH、Claude Code 的 finish 默认 completed；明确失败或停止仍可传 failed/stopped。三个后端的关闭、取消专属跟进与状态检查通过；真实 MCP 对已完成任务省略 status 的幂等关闭成功，没有派发新任务。

## 上下文实现

- 新增可配置的 `fatfish` ContextManager、SQLite 轮次/原文索引、候选版本和观察记录。内容寻址压缩保存一份明细，原 JSONL 和归档保留。真人消息按可信接收事件映射，重复消息不增加轮号；未匹配旧记录明确标注来源，内部回执不作真人发言。
- 已完成回合提供 UTC+8 时间、固定轮号、双方真实对话、实际 reasoning 和简短工具状态。原生 message 保留提交正文，最终回复、MCP 分段、媒体与未确认发送依据回执区分。当前工具链保持原协议。修复旧回执包装标签漏识别，22 条确定的旧回执转为内部记录，真人轮号保持不变。
- 新增延迟发现的 `turn_detail`，按 number 或内部 record_id 读取 dialogue/tools/reasoning/all；offset/limit 分页。继续沿用当前聊天、登记会话和群聊隔离。
- 摘要使用独立 system 与结构化来源材料；目标约 1500 中文字、最多 1800，输出 4096 tokens、60 秒、强制关闭本次摘要思考。近期仅用于纠正；新增过往正文与引用受覆盖范围限制。已知约定检查遗漏和无新来源的变更，来源核对的新约定独立保存。
- 候选先存版本、通过后激活，随后按稳定消息 ID 原子移出覆盖前缀。空输出、截断、错来源、越界、约定缺失或输入变化时保留旧版、原文及游标，并冷却 600 秒；不发布机械摘录作为新摘要。
- 完整输入的同一估算用于 admission、后台触发和 `/context`，包含私有记忆、思考、工具定义及网关时钟。窗口 65536、输出预留 8192、输入预算 57344；70% 水位 40140，整理目标约 28672–31539。保留近期约 6 个完整真人回合；硬上限只临时移出完整旧视图并提示未覆盖范围，当前工具链超限则明确报错。
- 网关改为两个并行请求，避免后台摘要独占旧单线程入口；保留时钟和账本。DeepSeek 携带 tools 时保留所有保留回合的 reasoning_content。依据[官方思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)并以真实模型工具续接核对。

## 验收与实际边界

相关 Go 上下文、来源、协议、轮次、存储和消息工具检查通过，两个新增 Python 查询/并发套件通过。Windows 全量 agent 检查另有全局技能路径、符号链接和 Seahorse 文件清理失败；相同失败项在干净 Linux 环境下全部通过。没有声称整个上游测试集全绿。

批处理回放：完整输入 53650 → 26977 tokens，一次覆盖 10 个旧回合，重启后原轮号与完整详情仍可读。失败、冷却、旧版保留、近期纠正和原子移出行为有定向检查。

三个正常真实用户回合的私人回放，均未创建新入站事件或发送 Telegram：

| 原轮号 | system | history | current | tools | clock | 合计 | 旧摘要版本/覆盖 |
|---|---:|---:|---:|---:|---:|---:|---|
| 534 | 4912 | 7519 | 5 | 3761 | 56 | 16253 | 0 / 525 |
| 535 | 4912 | 7901 | 38 | 3761 | 56 | 16668 | 0 / 525 |
| 536 | 4912 | 9512 | 8 | 3761 | 56 | 18249 | 0 / 525 |

最后一份完整输入经真实模型返回 HTTP 200，实际 prompt_tokens=11548；本地估算为保守估算，并非精确 tokenizer。真实工具协议测试返回 tool_calls 后，以原 ID 和实际 reasoning 续接，正常 stop。

真实模型的隔离正向检查：使用一轮实际聊天、单独重放会话、无旧摘要，生成 99 字候选，通过来源验证并保存版本 1。该结果属于隔离正向对照，不是线上摘要更新。复杂旧摘要与少量新料的强制测试仍出现错引和近期混入：错误候选保留为拒绝记录，人工审阅发现问题的一份也已在私人副本撤回。线上保留旧摘要版本 0、覆盖 525，当前输入低于启动水位，未强制替换。结构与已知来源可机械核对，不能据此宣称所有摘要语义已获证明。

线上 `turn_detail` 真实读取 536 的对话、工具和思考及其下一页成功；群聊读取私聊轮次被拒。最终索引为 512 条已匹配真人、22 条内部回执和 7 条待匹配旧来源记录。主进程已打开新索引和原事件库；health/ready 返回 200。

## 部署与回滚

最终程序 SHA256：`00a3931c7b3252c0a83fb64f8332bc9d02eacd36c39316536a0458ad83ac4f50`。

最终服务 PID：picoclaw 2880950、picoclaw-memory 2877671、picoclaw-budget 2874440，均 active。空闲切换主程序/工具约 1.81 秒，来源修正只重启主程序约 0.83 秒；网关重启通过 systemd 依赖同时触发过主服务重启。无进行中的真人会话被中断。

备份：`/var/backups/picoclaw/context-reform-20261104T150747Z/`。原二进制以 `0-picoclaw.gz` 保存，已完整解压核对原 SHA256。包含改前和迁移前配置、Python 模块、当前会话、数据库快照、正文及权限元数据。回滚入口（仅准备和语法检查，未执行）：

```sh
sudo python3 /var/backups/picoclaw/context-reform-20261104T150747Z/rollback_context.py
```

回滚恢复改前程序、配置和模块，保留后续聊天、原文归档、人格、记忆和数据库。操作指南只移除本次新增入口段落。第一件任务的协作原则与 finish 修复保留。

私人验收材料：`/var/tmp/fatfish-context-reform-20261104/`，包括 `audit/input-1.json` 至 `input-3.json`、`audit/one-round.txt`、`live-turn-536-*.json`、正向候选、拒绝版本、真实模型协议与用量、服务部署回执。私人正文、令牌和数据库未进入仓库。

表达经验模块未修改或部署，线上 `memory_learning.py` SHA256 仍为 `3aae0d0a747256e49104d565493c72e541e8b59abf2d9a682e6bd8912166d57f`。既有四个 Go 摘要/provider 修复完整保留并纳入本次源码；另一任务的表达经验及会话列表改动未纳入提交。

## 复核命令

```sh
systemctl is-active picoclaw picoclaw-memory picoclaw-budget
sha256sum /usr/local/bin/picoclaw
curl -fsS http://127.0.0.1:18790/health
curl -fsS http://127.0.0.1:18790/ready
cat /var/tmp/fatfish-context-reform-20261104/post-deploy-final.json
```

```sh
cd picoclaw
go test -tags goolm,stdjson ./pkg/agent ./pkg/contextledger -run 'TestFatfishReform|TestCandidate|TestKnownAgreement|TestRejectedCandidate|TestAcceptedCandidate|TestWrapped|TestRecentGrounded' -count=1
go test -tags goolm,stdjson ./pkg/providers/openai_compat ./pkg/session ./pkg/memory ./pkg/tools/integration -count=1
cd ../server-setup
python3 test-turn-history.py
python3 test-gateway-concurrency.py
python3 test-collaboration-finish.py
```
