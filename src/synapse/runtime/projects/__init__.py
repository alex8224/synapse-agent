"""Project-scoped runtime resources (P6)."""

from synapse.runtime.projects.identity import (
    ensure_project_identity,
    project_file_for,
    read_project_identity,
    write_project_identity,
)
from synapse.runtime.projects.runtime import (
    ProjectRegistry,
    ProjectRuntime,
    config_digest,
    mcp_pool_key,
)

__all__ = [
    "ProjectRegistry",
    "ProjectRuntime",
    "config_digest",
    "ensure_project_identity",
    "mcp_pool_key",
    "project_file_for",
    "read_project_identity",
    "write_project_identity",
]
