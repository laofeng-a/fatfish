# 共享人格与记忆文件写权限

2026-11-04，按jg确认的完整范围上线。前一次暂停时尚未修改线上权限；本次已实际部署。

## 变更

- `/var/lib/picoclaw-readers/profile/` 下的AGENT、USER、SOUL、HEARTBEAT，以及 `core.md`、`notes/MEMORY.md` 和工作区 `AGENTS.md`，全部设为0660。
- profile和notes目录由02750改为02770，允许主服务的原子编辑创建、替换临时文件；继承组仍为picoclaw。
- 工具服务增加 `SupplementaryGroups=picoclaw`，实际进程组为986、987。线上使用服务drop-in，仓库服务模板同步更新。
- Python记忆写入器对四个profile和MEMORY使用0660，日记仍为0640；core导出器使用0660。
- Go通用文件工具在原子替换已有、明确授予组写权限的普通文件时保留其权限。新文件及其他文件沿用0600默认值，软链接仍指向正本。

正文没有被本次权限操作覆盖。安装前重新备份并比对当前哈希，保留大肥鱼在此期间的编辑。core仍由结构化记忆数据库导出，修改其正文不等于修改数据库，后续导出仍会重建内容。

## 真实验收

- 主服务账号逐个以读写方式打开7个实际文件，通过；4个对应目录的临时文件创建、原子替换和清理通过。
- 工具账号按实际服务组配置逐个读写6个正本，通过；3个对应目录的原子替换通过。旧工作区AGENTS由主服务管理。
- 安装后的主程序以picoclaw账号，经原生 `mcp_reader_memory_note_read/write` 真实读取AGENT、追加一个换行、读回并恢复原文。保存后的文件为uid995、gid987、0660，主服务可读；最终原文哈希相同。
- 文件工具93项检查通过（包含子测试）；6项相关Python记忆检查通过。此前记忆学习13项检查也通过。
- 旧记忆授权测试有3项失败，未修改的线上模块对照同样失败；旧总策略套件亦有失败，未在本次权限任务中处理。本次没有调整授权行为，也不声称全部旧套件通过。

工具验收使用本地固定模型响应连接真实Go程序和MCP服务，没有真实模型或Telegram发送。

主服务和工具服务空闲时停启，停启与核验约5.7秒：

| 服务 | 原PID | 新PID |
|---|---:|---:|
| picoclaw | 2817087 | 2823990 |
| picoclaw-memory | 2797313 | 2823987 |
| picoclaw-budget | 1699687 | 1699687 |

主服务ready检查返回200。程序SHA256：`d457bbf0f8cdaf40d9e3dd6e7c5c4cb4b25893fb32c31d8eafb57427228a5f22`。原有并行摘要修复仍保留在本次构建中，相关4个工作树文件未纳入本次提交。

备份及回执：`/var/backups/picoclaw/memory-permissions-20261104T123839Z/`，包括原程序、Python模块、正文与权限元数据、部署前最新正文、`deployment.json`、`acceptance.json`、`native-profile-write.json`及相关测试日志。

```sh
systemctl is-active picoclaw picoclaw-memory picoclaw-budget
sudo cat /var/backups/picoclaw/memory-permissions-20261104T123839Z/acceptance.json
```

回滚程序时先停主服务与工具服务，再按backup.json恢复程序和两个Python模块、移除本次shared-memory.conf并daemon-reload，最后启动两项服务。权限是否恢复旧值单独决定，不恢复正文或数据库，以保留后续真实修改。
