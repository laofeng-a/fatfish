# 中文召回基线

用一份假的聊天、结构化记忆和日记，测量**现在这套词法检索**能找回多少。不读真实聊天，也不改 `history_find`、`chat_recall`、`memory_find`、`search_notes` 的实现。

语料在 `corpus.json`，问题在 `cases.json`。日记路径按运行当天的 UTC+8 日期加 `day_offset` 生成，所以换一天跑也不会写成“未来日记”。

三类问题：

- `exact`：查询是目标原文里的连续子串，应该命中。
- `paraphrase`：说法换了，查询不是目标原文的连续子串，按题意仍应该命中。
- `distractor`：不该命中。`expect_empty` 表示应返回空；`forbid_top1` 表示这个文档不该排第一。

指标：

- `hit@1` / `hit@5`：只统计精确和改写题。
- 改写命中率：改写题的 `hit@5`（JSON 里同时有 `paraphrase_hit_at_1`）。
- 干扰误报率：期望为空却有结果，或 `forbid_top1` 排在第一。
- 零结果：该工具所有题里返回空列表的个数。
- 耗时：每题重复 9 次，按工具做线性插值的 p50 / p95。每个工具先预热一次，不计入。耗时会随机器变化，`--check` 不比较耗时。

排序口径：`history_find` 用返回顺序；`chat_recall` 用 `matched_ids`（`around=0`）；`memory_find` 只看 `memories`；日记用 `MemoryNotes.search`。请求的 `limit` 是 8，用来观察前 5 名。

这次没有加耗时日志。日志要写进仍在本地修改的 `runtime_policy.py`，所以留空，避免和那些改动冲突。

在仓库根目录运行：

```sh
python3 server-setup/tests/recall_baseline.py
```

```bat
py -3 server-setup\tests\recall_baseline.py
```

结果写到 `baseline_results.json`。只核对命中、不重写文件：

```sh
python3 server-setup/tests/recall_baseline.py --check
```

结构检查（不覆盖结果文件）：

```sh
python3 server-setup/test-recall-baseline.py
```
