# P8：协议合规、跨平台与发布收口

> 状态：In progress；官方 SDK memory/subprocess/stdio 和 ACP 专项回归已有证据，发布门禁尚未全部关闭。  
> 前置条件：P0-P7 门禁全部通过。  
> 后续阶段：增量维护和受控 schema 升级。

## 1. 目标

以 P0 能力矩阵为准完成最终合规审查、跨平台验证、真实客户端互通、性能与安全收口，并形成可发布能力声明。

## 2. 合规范围

- 全部必选方法和语义。
- 全部声明 capability。
- JSON-RPC 非法参数、未知方法、request/notification、batch 行为。
- ContentBlock、SessionUpdate、stop reason 和错误联合类型。
- cancel、permission、disconnect 和资源释放竞争。
- stdio 编码、行分隔、buffer 上限和 stdout 纯净性。

## 3. 兼容性

- Windows Python 3.12/3.13。
- Linux Python 3.12/3.13。
- Zed 当前稳定版。
- 官方 Python SDK 驱动的独立 Client。
- 至少一个长会话、多 session、图片、MCP、permission、terminal 综合场景。

## 4. 性能与安全

- 队列、历史、分页、tool output、terminal output 和日志全部有界。
- 慢 Client 不阻塞 Agent runtime 或无限增长内存。
- 进程退出无存活子进程、连接、task 和数据库句柄。
- MCP/认证/Client env 和 headers 不出现在日志与导出中。

## 5. 发布门禁

- P0 矩阵无未解释项。
- ACP 专项测试、Ruff、全量 pytest、文档构建和 package build 通过。
- README/docs 包含安装、`synapse-acp` 配置、能力表和排障。
- SDK/schema 精确版本及升级策略已记录。
- 真实客户端验收结果写入进度台账。

当前未关闭：真实 MCP server、真实官方 Client 反向 filesystem/terminal 组合、Zed 互通、
Linux/Python 矩阵、notification/batch 语义、资源竞争审计，以及全量 pytest 中已有的 3 个非 ACP
tool-output/config 失败。ACP 专项当前为 55 passed；Ruff、MkDocs 和 package build 已通过。

## 6. 完成定义

只有本阶段全部门禁通过，`progress.md` 总体状态才能改为 `completed`。后续 SDK/schema 升级作为新变更执行 matrix diff、兼容测试和决策记录，不直接漂移依赖版本。

## 7. 风险与回滚

- 风险：真实 Client 对可选语义存在实现差异。缓解：协议合规与客户端兼容测试分层记录，不以单一客户端行为替代 schema。
- 回滚：保留现有 CLI/TUI 入口；ACP 发布问题可独立禁用 `synapse-acp`，不回滚 runtime 数据。
