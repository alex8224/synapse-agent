"""Global project catalog.

跨项目管理的基础数据层：Synapse 的会话数据按项目（workspace）隔离在
各自的 ``<workspace>/.synapse/sessions.sqlite`` 中，互不可见。本包在用户层
``~/.synapse/catalog.sqlite`` 维护一份只读投影：

- ``projects``：项目注册表。project_id 为稳定 UUID，workspace 路径可迁移；
- ``project_sessions``：会话元数据投影，复合主键 ``(project_id, thread_id)``；
- ``project_runs``：运行记录（TUI / CLI 每次启动一行）。

一致性策略：项目库是真源，catalog 是投影。
- 打开/使用项目时调用 :meth:`ProjectCatalog.sync_project` 全量对账（兜底）；
- TUI 每轮结束后增量 :meth:`ProjectCatalog.upsert_session`。

未来统一 agent 编排/管理可直接消费 ``projects`` + ``project_sessions`` 作为
“编排单元 + 工作单元 + 状态视图”。
"""

from synapse.projects.catalog import (
    CatalogSession,
    ProjectCatalog,
    ProjectInfo,
    ProjectRun,
    default_catalog_path,
    detect_git_metadata,
    project_name_for,
    synapse_project_dir,
)

__all__ = [
    "CatalogSession",
    "ProjectCatalog",
    "ProjectInfo",
    "ProjectRun",
    "default_catalog_path",
    "detect_git_metadata",
    "project_name_for",
    "synapse_project_dir",
]
