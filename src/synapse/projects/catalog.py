"""User-layer global project catalog (read-mostly projection).

Project-local session data stays in ``<workspace>/.synapse/sessions.sqlite``;
this module mirrors a bounded metadata projection into the user-layer
``~/.synapse/catalog.sqlite`` so tools, CLI, and future orchestration can see
every project without touching (or locking) the project databases.

Design notes:

- ``project_id`` is a stable UUID. ``workspace_path`` is UNIQUE and used as the
  join key for lookup, so a project can be renamed on disk without losing its
  id, and re-registering a moved path reuses the same row.
- The catalog is best-effort: every method degrades silently on corrupt state,
  and project databases remain the source of truth.
- WAL + busy_timeout follow the pattern used by the session search index so
  concurrent TUI / CLI processes can share the file.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.settings.config_paths import SYNAPSE_DIRNAME, user_config_dir

DEFAULT_CATALOG_FILENAME = "catalog.sqlite"

_MAX_PROJECTS = 500
_MAX_SESSIONS = 5_000
_GIT_TIMEOUT_SECONDS = 3.0


def default_catalog_path() -> Path:
    """User-layer catalog database (``~/.synapse/catalog.sqlite``)."""
    return user_config_dir() / DEFAULT_CATALOG_FILENAME


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _canonical(path: Path | str | None) -> Path:
    base = Path(path).expanduser() if path is not None else Path.cwd()
    return base.resolve()


def detect_git_metadata(workspace: Path | str | None) -> tuple[str | None, str | None]:
    """Best-effort ``(remote_url, branch)`` for a workspace. Never raises.

    Uses a bounded ``git`` subprocess probe; non-git dirs or missing git
    binaries return ``(None, None)``.
    """
    root = _canonical(workspace)
    remote: str | None = None
    branch: str | None = None
    git = shutil.which("git")
    if git is None:
        return None, None
    try:
        out = subprocess.run(
            [git, "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if out.returncode == 0:
            remote = out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - best-effort metadata
        remote = None
    try:
        out = subprocess.run(
            [git, "-C", str(root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if out.returncode == 0:
            branch = out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - best-effort metadata
        branch = None
    return remote, branch


@dataclass
class ProjectInfo:
    """One registered project (workspace) in the global catalog."""

    project_id: str
    workspace_path: str
    name: str
    git_remote: str | None
    git_branch: str | None
    created_at: str
    last_active_at: str
    session_count: int
    run_count: int
    metadata_json: str

    @property
    def metadata(self) -> dict[str, Any]:
        try:
            value = json.loads(self.metadata_json or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001 - corrupt metadata is not fatal
            return {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "workspace_path": self.workspace_path,
            "name": self.name,
            "git_remote": self.git_remote,
            "git_branch": self.git_branch,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "session_count": self.session_count,
            "run_count": self.run_count,
        }


@dataclass
class CatalogSession:
    """Session metadata projected from one project database."""

    project_id: str
    project_name: str
    workspace_path: str
    thread_id: str
    title: str
    model: str | None
    summary: str | None
    updated_at: str
    created_at: str
    tags: list[str]

    @property
    def global_id(self) -> str:
        """Stable cross-project reference ``<project_id>:<thread_id>``."""
        return f"{self.project_id}:{self.thread_id}"


@dataclass
class ProjectRun:
    """One recorded launch (TUI or CLI) of a project."""

    project_id: str
    run_id: str
    started_at: str
    finished_at: str | None
    mode: str
    thread_id: str | None
    exit_code: int | None


class ProjectCatalog:
    """SQLite-backed global project registry + session projection."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_catalog_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Multiple agents / TUI processes may share this file. WAL allows
        # concurrent readers + one writer; a long busy timeout absorbs peak
        # write contention instead of failing immediately.
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=15.0
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.Error:  # noqa: BLE001 - another process may hold the lock
            pass
        self._conn.execute("PRAGMA busy_timeout=15000;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=15000;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                git_remote TEXT,
                git_branch TEXT,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                session_count INTEGER NOT NULL DEFAULT 0,
                run_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_sessions (
                project_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                title TEXT NOT NULL,
                model TEXT,
                summary TEXT,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (project_id, thread_id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ps_project_updated "
            "ON project_sessions(project_id, updated_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ps_updated ON project_sessions(updated_at DESC)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                thread_id TEXT,
                exit_code INTEGER
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_project_started "
            "ON project_runs(project_id, started_at DESC)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def register_project(
        self,
        workspace: Path | str | None = None,
        *,
        name: str | None = None,
        git_remote: str | None = None,
        git_branch: str | None = None,
        detect_git: bool = True,
        now: str | None = None,
    ) -> ProjectInfo:
        """Upsert one project; returns its row.

        ``project_id`` is preserved when the workspace path was registered
        before. ``last_active_at`` is bumped on every registration.
        """
        root = _canonical(workspace)
        timestamp = now or _utcnow()
        if detect_git and git_remote is None and git_branch is None:
            git_remote, git_branch = detect_git_metadata(root)
        project_name = (name or "").strip() or root.name or str(root)

        existing = self._conn.execute(
            "SELECT project_id FROM projects WHERE workspace_path = ?", (str(root),)
        ).fetchone()
        if existing is not None:
            project_id = str(existing["project_id"])
            self._conn.execute(
                """
                UPDATE projects
                SET name = ?, git_remote = COALESCE(?, git_remote),
                    git_branch = COALESCE(?, git_branch), last_active_at = ?
                WHERE project_id = ?
                """,
                (project_name, git_remote, git_branch, timestamp, project_id),
            )
        else:
            project_id = uuid.uuid4().hex
            self._conn.execute(
                """
                INSERT INTO projects(
                    project_id, workspace_path, name, git_remote, git_branch,
                    created_at, last_active_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    str(root),
                    project_name,
                    git_remote,
                    git_branch,
                    timestamp,
                    timestamp,
                ),
            )
        self._conn.commit()
        info = self.get_project(project_id=project_id)
        assert info is not None  # freshly written
        return info

    def touch_project(self, workspace: Path | str | None = None, *, now: str | None = None) -> None:
        """Bump ``last_active_at`` without changing other fields."""
        root = _canonical(workspace)
        timestamp = now or _utcnow()
        self._conn.execute(
            "UPDATE projects SET last_active_at = ? WHERE workspace_path = ?",
            (timestamp, str(root)),
        )
        self._conn.commit()

    def get_project(
        self,
        workspace: Path | str | None = None,
        *,
        project_id: str | None = None,
    ) -> ProjectInfo | None:
        if project_id is not None:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        elif workspace is not None:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE workspace_path = ?",
                (str(_canonical(workspace)),),
            ).fetchone()
        else:
            return None
        return self._row_to_project(row) if row is not None else None

    def resolve_project(self, ref: str) -> ProjectInfo | None:
        """Resolve a user-supplied reference: id prefix, name, or path."""
        target = ref.strip()
        if not target:
            return None
        # Exact project_id or prefix.
        rows = self._conn.execute(
            "SELECT * FROM projects WHERE project_id = ? OR project_id LIKE ?",
            (target, f"{target}%"),
        ).fetchall()
        if rows:
            return self._row_to_project(rows[0])
        # Exact workspace path or path suffix.
        try:
            canon = str(_canonical(target))
            row = self._conn.execute(
                "SELECT * FROM projects WHERE workspace_path = ?", (canon,)
            ).fetchone()
            if row is not None:
                return self._row_to_project(row)
        except Exception:  # noqa: BLE001 - non-path references fall through
            pass
        # Name match (directory name or git repo name).
        rows = self._conn.execute(
            "SELECT * FROM projects WHERE name = ? OR git_remote LIKE ?",
            (target, f"%{target}%"),
        ).fetchall()
        return self._row_to_project(rows[0]) if rows else None

    def list_projects(self, *, limit: int = 100) -> list[ProjectInfo]:
        limit = max(1, min(int(limit), _MAX_PROJECTS))
        rows = self._conn.execute(
            "SELECT * FROM projects ORDER BY last_active_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def delete_project(
        self, workspace: Path | str | None = None, *, project_id: str | None = None
    ) -> bool:
        """Remove a project and its projected sessions/runs (project DB untouched)."""
        info = self.get_project(workspace=workspace, project_id=project_id)
        if info is None:
            return False
        self._conn.execute("DELETE FROM projects WHERE project_id = ?", (info.project_id,))
        self._conn.execute(
            "DELETE FROM project_sessions WHERE project_id = ?", (info.project_id,)
        )
        self._conn.execute("DELETE FROM project_runs WHERE project_id = ?", (info.project_id,))
        self._conn.commit()
        return True

    # ------------------------------------------------------------------
    # Session projection
    # ------------------------------------------------------------------

    def upsert_session(
        self,
        workspace: Path | str | None,
        *,
        thread_id: str,
        title: str | None = None,
        model: str | None = None,
        summary: str | None = None,
        updated_at: str | None = None,
        created_at: str | None = None,
        tags: list[str] | None = None,
        now: str | None = None,
    ) -> None:
        """Incrementally project one session row (used on turn completion)."""
        project = self.register_project(workspace, detect_git=False)
        timestamp = now or _utcnow()
        effective_updated = updated_at or timestamp
        existing = self._conn.execute(
            "SELECT updated_at FROM project_sessions WHERE project_id = ? AND thread_id = ?",
            (project.project_id, thread_id),
        ).fetchone()
        # Never regress `updated_at`; the project DB remains authoritative.
        if existing is not None and existing["updated_at"] > effective_updated:
            effective_updated = existing["updated_at"]
        self._conn.execute(
            """
            INSERT INTO project_sessions(
                project_id, thread_id, title, model, summary,
                updated_at, created_at, tags_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, thread_id) DO UPDATE SET
                title = excluded.title,
                model = COALESCE(excluded.model, project_sessions.model),
                summary = COALESCE(excluded.summary, project_sessions.summary),
                updated_at = MAX(project_sessions.updated_at, excluded.updated_at)
            """,
            (
                project.project_id,
                thread_id,
                (title or "").strip()[:240] or thread_id,
                model,
                summary,
                effective_updated,
                created_at or timestamp,
                json.dumps(tags or [], ensure_ascii=False),
            ),
        )
        self._conn.execute(
            "UPDATE projects SET session_count = "
            "(SELECT COUNT(*) FROM project_sessions WHERE project_id = ?), "
            "last_active_at = ? WHERE project_id = ?",
            (project.project_id, timestamp, project.project_id),
        )
        self._conn.commit()

    def sync_project(
        self,
        settings: Any,
        *,
        now: str | None = None,
    ) -> int:
        """Full reconciliation from one project's ``sessions.sqlite``.

        Opens the project database read-only and mirrors every session row.
        Returns the number of projected sessions. Never raises: corrupt or
        missing project databases simply project nothing.
        """
        self.register_project(settings.workspace, detect_git=False)
        path = settings.resolved_sessions_path()
        if not Path(path).is_file():
            return 0
        count = 0
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT thread_id, title, model, summary, updated_at, created_at, "
                    "tags_json FROM sessions"
                ).fetchall()
                for row in rows:
                    try:
                        tags = json.loads(row["tags_json"] or "[]")
                        if not isinstance(tags, list):
                            tags = []
                    except Exception:  # noqa: BLE001 - corrupt tags degrade to []
                        tags = []
                    self.upsert_session(
                        settings.workspace,
                        thread_id=str(row["thread_id"]),
                        title=str(row["title"] or ""),
                        model=row["model"],
                        summary=row["summary"],
                        updated_at=row["updated_at"],
                        created_at=row["created_at"],
                        tags=tags,
                        now=now,
                    )
                    count += 1
            finally:
                conn.close()
        except sqlite3.Error:
            return count
        return count

    def list_sessions(
        self,
        *,
        workspace: Path | str | None = None,
        project_id: str | None = None,
        limit: int = 200,
    ) -> list[CatalogSession]:
        """List projected sessions, optionally scoped to one project."""
        limit = max(1, min(int(limit), _MAX_SESSIONS))
        project = self.get_project(workspace=workspace, project_id=project_id)
        if project is not None:
            rows = self._conn.execute(
                """
                SELECT ps.*, p.name AS project_name, p.workspace_path
                FROM project_sessions ps
                JOIN projects p ON p.project_id = ps.project_id
                WHERE ps.project_id = ?
                ORDER BY ps.updated_at DESC LIMIT ?
                """,
                (project.project_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT ps.*, p.name AS project_name, p.workspace_path
                FROM project_sessions ps
                JOIN projects p ON p.project_id = ps.project_id
                ORDER BY ps.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_catalog_session(r) for r in rows]

    def search_sessions(
        self,
        query: str,
        *,
        workspace: Path | str | None = None,
        limit: int = 50,
    ) -> list[CatalogSession]:
        """Cross-project LIKE search over title and summary (bounded)."""
        needle = f"%{query.strip()}%"
        limit = max(1, min(int(limit), _MAX_SESSIONS))
        project = self.get_project(workspace=workspace) if workspace is not None else None
        if project is not None:
            rows = self._conn.execute(
                """
                SELECT ps.*, p.name AS project_name, p.workspace_path
                FROM project_sessions ps
                JOIN projects p ON p.project_id = ps.project_id
                WHERE ps.project_id = ? AND (ps.title LIKE ? OR ps.summary LIKE ?)
                ORDER BY ps.updated_at DESC LIMIT ?
                """,
                (project.project_id, needle, needle, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT ps.*, p.name AS project_name, p.workspace_path
                FROM project_sessions ps
                JOIN projects p ON p.project_id = ps.project_id
                WHERE ps.title LIKE ? OR ps.summary LIKE ?
                ORDER BY ps.updated_at DESC LIMIT ?
                """,
                (needle, needle, limit),
            ).fetchall()
        return [self._row_to_catalog_session(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """Aggregate counts used for activity views / orchestration basics."""
        projects = self._conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        sessions = self._conn.execute("SELECT COUNT(*) FROM project_sessions").fetchone()[0]
        runs = self._conn.execute("SELECT COUNT(*) FROM project_runs").fetchone()[0]
        active_projects = self._conn.execute(
            "SELECT COUNT(*) FROM projects WHERE substr(last_active_at, 1, 10) >= ?",
            (datetime.now(UTC).isoformat()[:10],),
        ).fetchone()[0]
        return {
            "projects": int(projects),
            "sessions": int(sessions),
            "runs": int(runs),
            "active_today": int(active_projects),
        }

    # ------------------------------------------------------------------
    # Run ledger (Phase 4: orchestration basis)
    # ------------------------------------------------------------------

    def record_run(
        self,
        workspace: Path | str | None,
        *,
        mode: str,
        thread_id: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        exit_code: int | None = None,
    ) -> str:
        """Record one launch (TUI / CLI) of a project. Returns run_id."""
        project = self.register_project(workspace, detect_git=False)
        run_id = uuid.uuid4().hex
        now = _utcnow()
        self._conn.execute(
            """
            INSERT INTO project_runs(run_id, project_id, started_at, finished_at,
                                     mode, thread_id, exit_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project.project_id,
                started_at or now,
                finished_at,
                mode,
                thread_id,
                exit_code,
            ),
        )
        self._conn.execute(
            "UPDATE projects SET run_count = run_count + 1, last_active_at = ? "
            "WHERE project_id = ?",
            (started_at or now, project.project_id),
        )
        self._conn.commit()
        return run_id

    def finish_run(
        self, run_id: str, *, exit_code: int | None = None, now: str | None = None
    ) -> bool:
        """Close an open run row (TUI exit path)."""
        cur = self._conn.execute(
            "UPDATE project_runs SET finished_at = ?, exit_code = ? WHERE run_id = ?",
            (now or _utcnow(), exit_code, run_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_runs(
        self,
        *,
        workspace: Path | str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[ProjectRun]:
        limit = max(1, min(int(limit), 500))
        project = self.get_project(workspace=workspace, project_id=project_id)
        if project is not None:
            rows = self._conn.execute(
                "SELECT * FROM project_runs WHERE project_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (project.project_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM project_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Row mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> ProjectInfo:
        return ProjectInfo(
            project_id=str(row["project_id"]),
            workspace_path=str(row["workspace_path"]),
            name=str(row["name"] or ""),
            git_remote=row["git_remote"],
            git_branch=row["git_branch"],
            created_at=str(row["created_at"] or ""),
            last_active_at=str(row["last_active_at"] or ""),
            session_count=int(row["session_count"] or 0),
            run_count=int(row["run_count"] or 0),
            metadata_json=str(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _row_to_catalog_session(row: sqlite3.Row) -> CatalogSession:
        try:
            tags = json.loads(row["tags_json"] or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:  # noqa: BLE001 - corrupt tags degrade to []
            tags = []
        return CatalogSession(
            project_id=str(row["project_id"]),
            project_name=str(row["project_name"] or ""),
            workspace_path=str(row["workspace_path"] or ""),
            thread_id=str(row["thread_id"]),
            title=str(row["title"] or ""),
            model=row["model"],
            summary=row["summary"],
            updated_at=str(row["updated_at"] or ""),
            created_at=str(row["created_at"] or ""),
            tags=tags,
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> ProjectRun:
        return ProjectRun(
            project_id=str(row["project_id"]),
            run_id=str(row["run_id"]),
            started_at=str(row["started_at"] or ""),
            finished_at=row["finished_at"],
            mode=str(row["mode"] or ""),
            thread_id=row["thread_id"],
            exit_code=row["exit_code"],
        )


def project_name_for(workspace: Path | str | None = None) -> str:
    """Convenience: directory name of a workspace (for CLI display)."""
    return _canonical(workspace).name or str(_canonical(workspace))


def synapse_project_dir(workspace: Path | str | None = None) -> Path:
    """Project-local state dir (``<workspace>/.synapse``)."""
    return _canonical(workspace) / SYNAPSE_DIRNAME
