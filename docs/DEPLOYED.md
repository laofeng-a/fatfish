# 线上现状

每次部署后更新本文件，并与部署一起提交。接手时先读这里，再用文末命令核对服务器；两者对不上，以服务器为准，并补记差异。

最后核对：2026-11-14 17:14:03（UTC+8）

## 主程序

| 项目 | 值 |
|---|---|
| 路径 | `/usr/local/bin/picoclaw` |
| SHA-256 | `2a4014208dd21d3a2d2ce21235b176bdc289be34aba633cb8b6cf2f7c2f7a7aa` |
| 对应源码 | commit `d350869`，约定退场 |
| 上线时间 | 2026-11-14 17:00 |
| 构建参数 | `-tags goolm,stdjson -ldflags '-s -w'`，`CGO_ENABLED=0 GOOS=linux GOARCH=amd64`，Go 1.25.11 |

复现部署二进制的哈希时还需保持构建路径和嵌入文件的实际换行一致：

- 构建没加 `-trimpath`，二进制里嵌入了构建机上的源码路径。本次是在主仓库目录下构建的。
- 嵌入的 `picoclaw/pkg/agent/fatfish_summary_prompt.txt` 是 CRLF 换行（14 个），仓库按 `.gitattributes` 存成 LF。

终态约定首次进入通过的摘要时记录 `terminal_since`，该版正文展示其终态。下一版通过时记录 `archived_version`，从状态、自动补约定及后续摘要材料中隐藏，并退出 1800 字预算。拒绝候选不推进退场计时，恢复 active 会清掉计时。原 JSON 保留在 `context_agreements.data`，恢复时可在维护流程中清除归档标记并核实恢复后的状态。使用原表的 JSON 字段，无需迁移数据库。

## Python 模块

`/opt/picoclaw/fatfish-v1/` 中运行用的模块与 `0faba94` 的 `server-setup/` 一致，只有换行符不同（服务器上是 CRLF）。例外：

- `memory-reader.py`：线上写的是实际的 second-brain 地址，仓库里是占位地址，这是有意的脱敏。
- 服务器上的部分 `test-*.py` 是旧副本，运行时不加载。
- 服务器上另有十几个一次性的探测、校验脚本（如 `verify-*.py`、`*-probe.py`），不在仓库里。

已写但未部署（在工作区，未提交）：`codex_bridge.py`、`conversation_access.py`、`memory_recall.py` 的改动，以及 `test-tool-efficiency.py`、召回基线测试。

## 服务与配置

- 服务：`picoclaw`、`picoclaw-memory`、`picoclaw-budget`。Go 主程序更新只需重启 `picoclaw`。
- 不在仓库里的线上配置：`/var/lib/picoclaw/.picoclaw/config.json`、`/etc/picoclaw-memory/runtime.json`、`/etc/systemd/system/picoclaw.service.d/tool-access.conf`。工具权限和沙箱的放开是有意的设计。
- 系统自动升级（`apt-daily-upgrade`）可能会重启这几个服务；PID 变化不代表部署变化，以哈希为准。
- journald 的 `/etc/systemd/journald.conf` 已设置 `SystemMaxUse=100M`。2026-11-14 17:13 重启后，启动记录确认实际最大值 100.0M，日志占用 75.3M。

## 摘要状态（最后核对时）

- 主会话：生效版本 80，覆盖到第 954 轮，最新轮号 1064，revision 为 3093。
- 第 80 号候选在上一版 `ab220008…` 部署后通过。当前独立约定共 7 条，均为 active，17:00 部署前后记录逐字一致。
- 三个服务均为 active，health/ready 正常，当前版本的 `rollback.py --check` 通过。17:00 部署时仅重启 picoclaw，17:13 磁盘清理后主服务 PID 为 334244，memory 为 310684，budget 为 310683。
- 约定退场的七组 Python 测试（123 项）与交接指定的三组 Go 测试全部通过，包含终态首次展示、下一次通过后归档、拒绝候选及 active 恢复用例。线上现有约定均为 active，终态退场流程的验证使用隔离测试数据。

## 回滚链

备份目录在 `/var/backups/picoclaw/`。每份备份只能回滚到它的上一版：脚本会先核对当前哈希，不匹配就拒绝执行。

| 当前版本 | 备份目录 | 回滚后版本 |
|---|---|---|
| `2a401420…` | `agreement-retirement-20261114` | `ab220008…` |
| `ab220008…` | `summary-repair-fix-20261114` | `df304e71…` |
| `df304e71…` | `summary-home-fix-20261114` | `9e4521ba…` |
| `9e4521ba…` | `summary-recovery-20261111` | `b7ee8f55…` |

先运行 `sudo python3 /var/backups/picoclaw/<目录>/rollback.py --check` 预检，去掉 `--check` 才会真正执行。回滚会保留上线后的聊天和账本数据。

## 磁盘维护

2026-11-14 17:13 按已确认的固定清单删除 169 项，共 2.180 GiB，分别为 67 个旧 PicoClaw 备份、48 个早期 Fatfish 备份、10 个上传暂存目录和 44 个临时文件及目录。上表四份回滚备份均已保留，旧主程序哈希逐项核对通过。当前主程序 SHA-256 见上表，回滚预检通过。

执行 `journalctl --vacuum-size=100M`，备份并原子更新 journald 配置后重启 `systemd-journald`，再执行 `apt clean`。根分区使用率从 89% 降至 58%，已用空间从 7.7G 降至 5.0G，可用空间从 1.1G 增至 3.7G，按实际字节统计共释放 2.631 GiB。三个应用服务均为 active，health/ready 正常。

journald 原配置与完整维护回执保存在 `/var/backups/picoclaw/agreement-retirement-20261114/disk-cleanup-20261114/`，文件为 `journald.conf.before` 和 `receipt.json`。本地逐项清单与回执位于仓库同级的 `server-setup/state/disk-cleanup-20261114/`。

## 核对命令（在服务器上执行）

```bash
sudo sha256sum /usr/local/bin/picoclaw
systemctl is-active picoclaw picoclaw-memory picoclaw-budget
curl -fsS http://127.0.0.1:18790/ready
cd /opt/picoclaw/fatfish-v1 && sudo sha256sum *.py
```

Python 模块的哈希要先把 CRLF 换成 LF，再和 `git show <commit>:server-setup/<文件>` 对比。

## 部署记录

| 时间 | 二进制 | 源码 | 内容 |
|---|---|---|---|
| 2026-11-14 17:00 | `2a401420…` | `d350869` | 终态约定在一版通过的摘要里展示，下一版通过后归档，原记录保留 |
| 2026-11-14 16:01 | `ab220008…` | `0faba94` | 快照 revision 重新对齐、修正被跳过时记日志、自动补上漏写的已有约定 |
| 2026-11-14 10:54 | `df304e71…` | 包含在 `848ca9e` 中 | 约定出处核对不上时不再拒绝整份摘要；一并上线 11-13 的引号与约定改动 |
| 2026-11-11 | `9e4521ba…` | 未单独提交 | 摘要定向修正和 canonical_homes（覆盖到第 752 轮，人工激活版本 60） |
