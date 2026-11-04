# 人格去重、记忆工具修复与64K核验

2026-11-04 20:19（UTC+08）最终部署完成。

## 文件调整

修改前备份位于服务器 `/var/backups/picoclaw/profile-tools-20261104T112224Z/`，包含原文件、权限、哈希及验收回执。私人正文和运行凭据未进入源码仓库。

| 文件 | 修改前字节 | 修改后字节 | 结果 |
|---|---:|---:|---|
| `profile/SOUL.md` | 7834 | 7885 | 指代段合入拿不准时询问；低于8000字节上限 |
| `profile/USER.md` | 5591 | 4061 | 删除指定重复项，合并主动聊天两条并保留2026-10-25出处 |
| `profile/AGENT.md` | 2392 | 2392 | 原文保留；工具验收的临时换行已还原 |
| `profile/HEARTBEAT.md` | 3959 | 3959 | 原文保留 |
| `workspace/AGENTS.md` | 1523 | 2070 | 更新操作授权、自动唤醒、个人记忆与共享记忆工具的实际能力说明 |
| `config.json` | 4949 | 4949 | 已为65536，本次内容哈希未变 |

`profile/` 正本位于 `/var/lib/picoclaw-readers/profile/`，保持 `picobudget:picoclaw 0640`。个人文件通过版本化写入并读回，主服务账号读取检查通过。USER 中另外的个人评价段完整保留。

## 工具故障与修复

Python MCP 服务实际注册了 `memory_note_read/write`。错误发生在 Go 执行入口：`ToolRegistry.Get` 按 schema TTL 隐藏延迟工具，而执行入口也调用该方法，导致模型保留的已知工具名在 TTL 过期后被报告为 `tool not found`。

`registry.go` 将执行查找与说明可见性分开：精确命中已注册名称即可执行，仍检查 agent allowlist、参数和原有回合策略。未注册名称仍失败。执行不会顺带加载 schema，按需发现与TTL机制继续工作。

使用安装后的主程序、真实 MCP 服务和主服务账号 `picoclaw` 验收。模型响应由本地固定测试端提供；没有调用真实模型或发送Telegram消息。实际执行序列：

1. `mcp_reader_memory_note_read` 读取AGENT，2392字节。
2. `mcp_reader_memory_note_write` 带最新revision追加一个换行，返回 `saved` 和 `readback_verified=true`。
3. 再次原生读取，正文与新revision均符合预期。
4. 原生写入恢复原文，最终哈希与修改前相同。

写入后的文件为uid995、gid987、0640，主服务可读。首次模型请求只包含18个工具说明，没有提前加载记忆读写说明。冷工具名与TTL过期的回归检查均通过。

## 窗口与服务

本次开始时 `agents.defaults.context_window` 已是65536，因此没有重复改写config。新进程与配置哈希已核对；用同一安装程序、相同模型名及生产预算参数运行隔离CLI的原生 `/context`，返回：

```text
Used: ~6295 / 65536 tokens (9%)
Compress at: 57344 tokens
Summarize at: 42598 tokens
```

输出预留仍为8192。以上是配置加载与预算计算验收，没有据此声称真实长聊天不会再压缩。Telegram中发送 `/context` 可复查当前会话预算。

最终部署前确认没有正在处理的真人回合或后台唤醒，只重启主服务；最终停启与核验耗时约3.6秒。

| 服务 | 部署前PID | 部署后PID |
|---|---:|---:|
| picoclaw | 2813196 | 2817087 |
| picoclaw-memory | 2797313 | 2797313 |
| picoclaw-budget | 1699687 | 1699687 |

三项服务均active。最终主程序SHA256：`53b9193ea9b92383597be080b47bcdb8a53e62a9d538dcc308ccb7271e8e142f`。

相关检查通过：工具注册/发现55项、上下文与回合策略86项（3项显式验收默认跳过）、模型兼容层93项，数量包含子测试。

## 部署记录与复查

部署过程中检测到另一项摘要修复更新了线上程序和本地4个agent/provider文件，保护检查阻止覆盖旧版。最终从保留这些改动的工作树重新编译；本次提交只包含工具修复及其记录，并行文件未纳入本次提交。并行源码哈希记录在私有验收目录 `parallel-source-hashes.json`，部署前的完整程序另备份到 `pre-merged-runtime/picoclaw`。

首次部署尝试因启动日志验收条件不适用而自动回滚，最终采用进程/程序哈希、原生工具调用与 `/context` 验收。传输前还遇到磁盘满；清理了与本地哈希相同的旧临时构建物，保留原文件备份和日志，最后约230MiB可用。

私有验收目录为 `/var/tmp/fatfish-profile-tools-20261104/`；结果同时复制到备份目录：

- `profile-edits.json`、`final-verification.json`
- `native-write-latest.json`、`native-write-latest.log`
- `context-budget-receipt.json`、`merged-deployment.json`
- 三组测试日志及结果

可复查：

```sh
systemctl is-active picoclaw picoclaw-memory picoclaw-budget
sudo cat /var/backups/picoclaw/profile-tools-20261104T112224Z/native-write-latest.json
sudo cat /var/backups/picoclaw/profile-tools-20261104T112224Z/context-budget-receipt.json
```

仅回滚本次程序修复时，恢复 `pre-merged-runtime/picoclaw` 并重启主服务；不回滚聊天、数据库或人格文件。人格如需恢复，先读最新revision，再通过版本化记忆接口写入备份正文并读回。
