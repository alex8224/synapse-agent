"""Stable per-workspace project identity (``<workspace>/.synapse/project.json``).

The catalog currently keys projects by path with a stable UUID; moving a
directory re-registers under a new path but keeps the id.  ``project.json``
makes the id durable inside the workspace itself so a moved directory can be
recognised as the same project without needing the catalog's path join.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from synapse.settings.config_paths import SYNAPSE_DIRNAME

PROJECT_FILE = "project.json"
SCHEMA_VERSION = 1


def project_file_for(workspace: Path | str) -> Path:
    base = Path(workspace).expanduser()
    return base / SYNAPSE_DIRNAME / PROJECT_FILE


def read_project_identity(workspace: Path | str) -> dict[str, Any] | None:
    """Read project.json; return None when absent or corrupt."""
    path = project_file_for(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    return data


def write_project_identity(
    workspace: Path | str,
    project_id: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Persist (or refresh) project.json inside the workspace."""
    path = project_file_for(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_project_identity(workspace) or {}
    data["schema_version"] = int(SCHEMA_VERSION)
    data["project_id"] = project_id
    if name:
        data["name"] = name
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return data


def ensure_project_identity(
    workspace: Path | str,
    *,
    catalog_project_id: str | None = None,
    name: str | None = None,
) -> str:
    """Return the stable project id for a workspace, creating it if needed.

    Resolution order:
    1. Existing ``project.json`` id wins.
    2. Otherwise use the catalog-provided id (re-registering a moved path).
    3. Otherwise mint a fresh UUID and persist it.
    """
    existing = read_project_identity(workspace)
    if existing is not None:
        return str(existing["project_id"])
    project_id = catalog_project_id or str(uuid.uuid4())
    write_project_identity(workspace, project_id, name=name)
    return project_id


def reconcile_project_identity(
    workspace: Path | str,
    project_id: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Force ``project.json`` to agree with an authoritative project id.

    :func:`ensure_project_identity` deliberately lets an existing
    ``project.json`` win over ``catalog_project_id``, so a *moved* directory can
    keep its id even though the catalog row for the old path cannot be joined to
    the new one. This is the opposite case: the caller already resolved
    ``project_id`` for *this* workspace path (e.g. a catalog lookup keyed by that
    path), so a disagreement means ``project.json`` is stale and must be
    rewritten. An id that already matches is left untouched, other keys are
    preserved.
    """
    existing = read_project_identity(workspace)
    if existing is not None and str(existing.get("project_id") or "") == str(project_id):
        return existing
    return write_project_identity(workspace, project_id, name=name)
