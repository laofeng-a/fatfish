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
- 上下文窗口于 2026-11-03 调为 131072，输出预留 8192；摘要比例阈值仍为 65%。
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

## 本地表情

将 `workspace-tools/emote_pick.py` 和 `emote_send.py` 放到工作区 `tools/`。优先一次执行 `python3 tools/emote_send.py --pick --tag 标签`，也支持 `--query`、`--kind` 和 `--dry-run`。

`pick` 只挑选，不提前记账；成功发送后以真实消息 ID 记一次。库未用满一半时，指定标签没有新图就返回无可选项，换条件后再选。显式指定已用编号也不能绕过这条规则。脚本与原生表情发送共用工作区 `state/emote-used.json`；失效的旧目录编号不计入半库使用率。

原生 `send_file` 对 `assets/emotes` 内的 `.webm` 使用贴纸、`.jpg/.png` 使用图片、`.gif/.mp4` 使用动画；普通目录的文件和视频沿用原来的类型。多张库内表情逐张发送和记账，普通图片组行为沿用原实现。

本次发布和真实回执见 [2026-11-03 发布记录](RELEASE-2026-11-03.md)。

## 源码导入边界

这次整理没有修改运行系统。Git 历史从整理后的源码快照开始；原来散落的部署、回放、屏幕截图和私人对话记录保留在原目录，不作为源码提交。

上游示例和测试中存在公开的示例 Token 字符串，应与真实部署凭据区分。实际凭据只放在受限文件或环境中，避免直接写入源码。
