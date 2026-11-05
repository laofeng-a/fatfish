# 当前事件来源修复（2026-11-05）

已上线。针对消息 5622“哔哔哔”和 5624“？”被模型当成时钟刷新的问题。原轮、实际输入样本及投递回执证实两轮均由真人消息触发，原话完整，期间没有对应自动唤醒；当时输入估算约 25K，低于 57344 的输入预算。

## 改动

- 新增 `picoclaw/pkg/agent/fatfish_turn_event.go`，每次模型调用前依据真实 dispatch 元数据生成 `[fatfish_turn_event]`：事件类型、来源、消息号及当前阶段。覆盖真人消息、自主活动、任务巡检、额度重置、日记维护、闹钟、cron、执行端回执和异步工具回调。回调即使沿用原真人消息号，也优先按回调来源处理。
- 同轮工具结果续接保留原触发来源，steering 到达后按最新真人消息号更新；标识只属于请求视图，不写回聊天原文或人格文件。任务规范替换、工具折叠之后注入，进入预算检查、实际请求采样及任务请求审计。
- `agent_loops.py` 的既有 `loop_context` 查询补回真实 `wake_kind`，从已存在的唤醒记录读取，区分任务巡检和接续同一任务的额度唤醒；没有新增排程或表结构。
- 网关 `budget-gateway.py` 将时间块明确标为日期、时刻和相对时间的附注；Go 输入预算估算同步更新。保留现有尾部时钟缓存排列；此前一次位置对照未稳定复现问题，未把移动时钟当作已经证实的根治方法。
- 真人事件提示采用正向目的：“当前回应对象是这条真人消息的发送者，接续他的本轮原话；先前的等待安排随眼前对话判断。”

排程、汇报开关、撤销行为、原始聊天、思考与工具记录、人格、记忆、上下文窗口均保持原有行为。运行时增加一个小的来源标识，预算检查按实际内容计算。

## 验证

- Windows 与 Linux 验证实际 provider 在两次模型调用中均收到正确来源和消息号；工具结果续接保持来源，消息原文和工具协议不变，来源视图不污染历史。Linux 相关测试及子项 21 个通过；另一个需要单独私有材料的历史回放测试跳过。
- Python：任务循环 19、时钟/缓存 7、DeepSeek 兼容 8、网关并发 1，合计 35 项通过。验证真人短消息和来源块不会被时钟刷新覆盖，缓存前缀、工具调用配对与图片保持完整。
- 新 Go 测试二进制对真实输入样本生成来源视图后，经已运行的新网关回放两轮；未创建 Telegram 入站、未执行发送工具、未投递验收消息。
- “哔哔哔”回放结果：“（先探出头）哔哔哔什么，我在呢。”；“？”回放结果承认上一轮漏接并回应当前用户。两轮均以 `stop` 正常结束，未再输出“时钟而已，他没出声”。这是有限真实样本验证，不宣称模型以后不会误判。
- 线上 `loop_context` 实际读回 `autonomy`、`task` 来源；模块哈希匹配，health/ready 正常。

复验入口：

```sh
python3 server-setup/test-agent-loops.py
python3 server-setup/test-deepseek-cache.py
python3 server-setup/test-deepseek-gateway.py
python3 server-setup/test-gateway-concurrency.py
cd picoclaw
go test -tags goolm,stdjson ./pkg/agent -run 'TestFatfish(TurnEvent|Loop|Steering|DiaryInjection|History)' -count=1
```

真实材料投影测试通过 `FATFISH_EVENT_REPLAY_DIR` 按需读取私有样本，默认跳过；实际材料和响应不进入源码仓库。

## 部署与回滚

主程序 `/usr/local/bin/picoclaw`：`93e687f9c98bfaefdcd900b0756389dcec8677d6d4574add4b872f4dc8cb8040` → `e95e5e10309f8674730bf6566240ca0f3d815913c3484e3e8ecde0dbdcfcd795`。

仅更新主程序及 `agent_loops.py`、`budget-gateway.py`，在无活动真人回合和 wake 时重启三个加载组件。切换到健康就绪用时 **3.39 秒**；主进程 PID 2987118，记忆服务 2987113，模型网关 2987114。部署保留服务器原有网关配置和其他代码，只对现有时间文本追加用途说明。

备份与回执：`/var/backups/picoclaw/event-source-20261105/`；本地私有证据：仓库外 `server-setup/state/event-source-20261105/evidence/`。根分区空间不足容纳两个展开的主程序，先校验持久化压缩备份，再在服务停止期间替换可执行文件；失败会自动还原。

```sh
sudo python3 /var/backups/picoclaw/event-source-20261105/rollback.py
```

回滚先检查当前主程序及两个模块哈希，遇到后续更新即拒绝覆盖；不回退数据库和上线后记录。脚本已语法校验，未执行回滚。两个旧验收数据库副本做了校验哈希的无损 gzip 归档，记录于 `storage-archive.json`，没有改动线上数据库。

交付后清理服务器临时构建与回放目录；本地构建包、测试二进制和分片集中到“待删除-大肥鱼中间文件-20261105”下的本次子目录，供用户手动删除。备份与证据保留。
