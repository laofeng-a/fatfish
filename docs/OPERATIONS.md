# 维护说明

## 组件与数据

典型 Linux 布局：

| 位置 | 用途 |
|---|---|
| `/usr/local/bin/picoclaw` | Go 主程序 |
| `/opt/picoclaw/fatfish-v1/` | `server-setup/` 中的线上 Python 模块 |
| `/var/lib/picoclaw/.picoclaw/config.json` | 主程序私有配置 |
| `/etc/picoclaw-memory/runtime.json` | 工具运行配置 |
| `/etc/picoclaw-memory/settings.json` | 工具中转凭据位置和上游地址 |
| `/etc/picoclaw-budget/settings.json` | 模型中转配置与凭据文件位置 |
| `/var/lib/picoclaw-readers/profile/` | AGENT、SOUL、USER、HEARTBEAT 正本 |
| `/var/lib/picoclaw-readers/notes/` | 个人记忆与日记 |
| `/var/lib/picoclaw-readers/runtime.sqlite` | 会话记录、任务与记忆运行状态 |

上述私有配置和数据均不在仓库内。服务模板中的 `/etc/picoclaw/private.env` 用于提供实例自己的 `FATFISH_OWNER_CHAT` 等环境变量。工作区路径和服务账号按实例配置。

Windows 连接器安装目录通常为 `%USERPROFILE%\.codex\fatfish-codex`。SSH 登录、Codex 登录态、运行状态与机器配置由本机保管。`codex-control/` 只收录代码和离线测试，不收录这些状态文件。

## 本次导入的配置特点

- MCP 工具发现开启，reader/deepseek 工具按需加载；BM25 与名称正则入口启用。
- 短 AGENT 常驻，实际操作流程位于工作区 `guides/OPERATIONS.md`，需要时读取。
- 上下文窗口 32768，输出预留 8192。此次工具发现改动没有扩大窗口。
- 周期安排、人格和长期记忆是实例私有状态，源码仓库不替代这些状态的备份。

`server-setup/memory-reader.py` 的个人 Second Brain 地址已改为通过 `FATFISH_SECOND_BRAIN_ORIGIN` 配置；默认示例域名不可连接。对应 `settings.json` 中的 `origin` 应与配置地址一致。

## 修改与发布

1. 先核对线上文件与源码版本，明确这次要改的问题。
2. 在本机源码仓库修改并运行相关测试。涉及真实发送的验收单独安排。
3. 改前备份涉及的源码、配置与 profile 版本；数据库备份单独保管。
4. 部署涉及的文件，只重启实际加载这些文件的服务。
5. 核对服务状态、日志、实际读取与真实回执，留回滚入口。
6. 在源码仓库提交最终变化；凭据、聊天和运行记录留在私有数据备份中。

人格写入沿用 `memory_note_read/write` 或 `MemoryNotes.write`，带最新 `expected_revision` 并读回。源码仓库的模板不覆盖线上后续修订。

回滚代码或配置时，保留上线后新增的聊天、任务和记忆数据；需要恢复数据库时另行明确恢复范围。

## 源码导入边界

这次整理没有修改运行系统。Git 历史从整理后的源码快照开始；原来散落的部署、回放、屏幕截图和私人对话记录保留在原目录，不作为源码提交。

上游示例和测试中存在公开的示例 Token 字符串，应与真实部署凭据区分。实际凭据只放在受限文件或环境中，避免直接写入源码。
