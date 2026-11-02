# 配置样例

这些文件用于说明必要字段，不包含 Telegram 通道、搜索配置、全部平台凭据或现有个人数据。

- `picoclaw.example.json`：主程序与工具发现示例。通道配置按当前 PicoClaw 的 `channel_list` 格式添加，参考 `picoclaw/config/` 和上游文档。
- `memory.settings.example.json`：个人记忆服务凭据位置。实际地址与 `FATFISH_SECOND_BRAIN_ORIGIN` 对齐。
- `runtime.example.json`：运行状态位置与调度开关示例。维护任务编号来自当前数据库，不应复制其他实例的编号。
- 模型中转和搜索服务的完整设置字段分别见 `server-setup/budget-gateway.py`、`server-setup/deepseek-search-mcp.py`。

所有 `REPLACE_WITH_*`、`YOUR_*` 和 `.example.invalid` 都需要按实例填写。真实文件保存在部署环境或本地忽略目录中。
