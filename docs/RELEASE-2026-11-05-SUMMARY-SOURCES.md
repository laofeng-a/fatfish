# 摘要来源与生成质量补充修复

2026-11-05 04:15（UTC+08）上线，接续同日上下文改造。此前“复杂旧摘要的新候选仍被拒绝”的限制，现已补上来源组织和引文生成修复，并以正常水位的真实历史批次通过验证。

## 改动

旧策略的版本 0 摘要此前被装进 previous_accepted，旧文中的引号也能作为自己的来源证明。现在将它标为 legacy_unverified_reference，仅从实际接收及确认送达记录找回可核对的引文；普通聊天视图同样标明旧策略记录尚未经新摘要验收，以较新原话和人格、记忆正本校正。原存储正文及覆盖游标保持原值。

摘要材料改为双方对话对象，每项同时携带 turn、speaker、text，模型沿同一对象取原话。过往区保留实际思考和工具结论；近期校正区只提供真实对话，避免整段思考和工具过程诱使模型重讲近期经历。普通聊天原有的思考保留和按轮详情查询继续工作。

summary 可用 `{{q1}}` 等引用 quotes；程序验证来源后，加入实际说话人与逐字原文。其他直接引号仍须对应来源，没有放宽错引、边界及已知约定的检查。

正文引文未能匹配时，先保存拒绝版本，再允许一次带具体错误片段的定向修正。两次调用共用同一个 60 秒期限，每次输出上限仍为 4096 tokens；修正输入也检查完整预算。有新输入、版本变化、超预算或再次失败则保留旧版和原文、进入 600 秒冷却。模型服务错误、空输出、截断等不走这次引文修正。已核对的新约定仍独立保存。

## 验收

相关 agent 检查和完整 contextledger 套件通过，覆盖旧摘要不能自证引文、恢复原说话人/轮号、近期材料不带整段思考、引文编号渲染、低于水位不调用模型，以及修正成功/两次失败时的版本、原文、游标与冷却。

真实模型验收采用私有副本中的受控历史回放：将副本旧覆盖点设为 480，重放已记录的真实会话，未改线上游标、未创建新入站事件、未发送 Telegram 验收消息。

| 项目 | 实际结果 |
|---|---:|
| 整理前完整输入估算 | 43860 tokens |
| 后台启动水位 | 40140 tokens |
| 本批真人回合 | 30 |
| 实际覆盖范围 | 481–518，保留原固定轮号 |
| 近期校正回合 | 6 |
| 通过的渲染正文（含约定） | 1129 字 |
| 保存并激活的副本版本 | 4 |
| 整理后完整输入估算 | 27115 tokens |
| 整理目标上界 | 31539 tokens |

副本保留了前三份拒绝版本；最终候选首次生成即通过，没有触发定向修正。定向修正分支通过单独的行为测试，不能将该分支自测说成此次真实模型已用过。验收包括逐字来源检查及正文审阅，不代表已机械证明所有自然语言语义。

线上主人会话继续保留旧版本 0、覆盖点 525，等待实际输入达到水位后再按新流程更新。没有为了展示摘要成功而把副本游标复制回线上。

## 部署

只替换 `/usr/local/bin/picoclaw`，配置未修改。仍为 fatfish 模型、65536 窗口、8192 正常输出预留，输入预算 57344。

程序 SHA256：`c973e6e58033f4e969e1e3ce8674a387af15367cb6df57e8bb4f9768bd5b8bf5`。

主进程 PID 2880950 → 2894212，空闲切换约 1.33 秒，health/ready 均 200。工具服务 PID 2877671、网关 PID 2874440 保持，三项均 active。未修改人格/记忆正本、Python 工具和表达经验模块；并行会话列表及表达经验工作树修改未纳入提交。

备份：`/var/backups/picoclaw/summary-sources-20261104T201451Z/`，包含旧程序压缩包（完整解压校验 SHA256）、配置参考、索引快照和部署回执。回滚脚本已准备并检查语法，未执行：

```sh
sudo python3 /var/backups/picoclaw/summary-sources-20261104T201451Z/rollback_summary_sources.py
```

只回滚本次主程序，保留后续聊天、记忆、配置及摘要版本。

私人材料位于 `/var/tmp/fatfish-summary-materials-20261105/`：`acceptance.json`、`deployment.json`、`normal-generation-material.json`、`normal-repaired-candidate.json`、`all-candidates.json` 及回放日志和输入样本。正文和数据库未提交仓库。

```sh
systemctl is-active picoclaw picoclaw-memory picoclaw-budget
sha256sum /usr/local/bin/picoclaw
curl -fsS http://127.0.0.1:18790/ready
cat /var/tmp/fatfish-summary-materials-20261105/acceptance.json
```

```sh
cd picoclaw
go test -tags goolm,stdjson ./pkg/contextledger -count=1
go test -tags goolm,stdjson ./pkg/agent -run 'TestFatfishReform|TestModelMaterials|TestLegacy|TestQuoteReferences' -count=1
```
