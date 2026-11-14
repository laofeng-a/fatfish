# 线上现状

每次部署后更新本文件，并与部署一起提交。接手时先读这里，再用文末命令核对服务器；两者对不上，以服务器为准，并补记差异。

最后核对：2026-11-14 16:44:19（UTC+8）

## 主程序

| 项目 | 值 |
|---|---|
| 路径 | `/usr/local/bin/picoclaw` |
| SHA-256 | `ab2200086cf796f46f95e266fc80b7fa657964c6060e66cad15c824d94b76c1c` |
| 对应源码 | commit `0faba94` |
| 上线时间 | 2026-11-14 16:01 |
| 构建参数 | `-tags goolm,stdjson -ldflags '-s -w'`，`CGO_ENABLED=0 GOOS=linux GOARCH=amd64`，Go 1.25.11 |

从 `0faba94` 重新构建会得到不同的哈希，原因有两个，源码内容是相同的：

- 构建没加 `-trimpath`，二进制里嵌入了构建机上的源码路径。本次是在主仓库目录下构建的。
- 嵌入的 `picoclaw/pkg/agent/fatfish_summary_prompt.txt` 当时是 CRLF 换行，仓库按 `.gitattributes` 存成 LF。

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

## 摘要状态（最后核对时）

- 主会话：生效版本 80，覆盖到第 954 轮，最新轮号 1064，revision 为 3093。
- 新版部署后第 80 号候选已通过。当前独立约定共 7 条，均为 active。
- 三个服务均为 active，health/ready 正常；当前版本的 `rollback.py --check` 通过。三段回滚备份的主程序哈希与部署记录逐项一致。

## 回滚链

备份目录在 `/var/backups/picoclaw/`。每份备份只能回滚到它的上一版：脚本会先核对当前哈希，不匹配就拒绝执行。

| 当前版本 | 备份目录 | 回滚后版本 |
|---|---|---|
| `ab220008…` | `summary-repair-fix-20261114` | `df304e71…` |
| `df304e71…` | `summary-home-fix-20261114` | `9e4521ba…` |
| `9e4521ba…` | `summary-recovery-20261111` | `b7ee8f55…` |

先运行 `sudo python3 /var/backups/picoclaw/<目录>/rollback.py --check` 预检，去掉 `--check` 才会真正执行。回滚会保留上线后的聊天和账本数据。

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
| 2026-11-14 16:01 | `ab220008…` | `0faba94` | 快照 revision 重新对齐、修正被跳过时记日志、自动补上漏写的已有约定 |
| 2026-11-14 10:54 | `df304e71…` | 包含在 `848ca9e` 中 | 约定出处核对不上时不再拒绝整份摘要；一并上线 11-13 的引号与约定改动 |
| 2026-11-11 | `9e4521ba…` | 未单独提交 | 摘要定向修正和 canonical_homes（覆盖到第 752 轮，人工激活版本 60） |
