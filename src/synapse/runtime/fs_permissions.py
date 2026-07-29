"""FilesystemPermission helpers for create_deep_agent(permissions=...).

Important: deepagents currently rejects permissions when the backend supports
command execution (LocalShellBackend / SandboxBackendProtocol). This project
always uses LocalShellBackend, so ``build_filesystem_permissions`` returns
None unless ``force=True`` (for non-shell backends / experiments).

Product readonly mode should use harness tool exclusion instead
(``apply_harness_exclusions`` / ``AGENT_READONLY``).
"""

from __future__ import annotations

from typing import Any


def build_filesystem_permissions(
    *,
    enabled: bool = False,
    readonly: bool = False,
    deny_paths: list[str] | None = None,
    force: bool = False,
    shell_backend: bool = True,
) -> list[Any] | None:
    """Build deepagents FilesystemPermission rules.

    Paths must be POSIX-style and start with '/'. With LocalShellBackend +
    virtual_mode, workspace roots are typically exposed under '/'.

    Returns None when ``shell_backend`` is True (default), because permissions
    + execute backends are unsupported by FilesystemMiddleware.
    """
    if shell_backend and not force:
        # Avoid hard failure at agent build time on LocalShellBackend.
        return None

    if not enabled and not readonly and not deny_paths:
        return None

    from deepagents import FilesystemPermission

    rules: list[Any] = []

    if readonly:
        # Deny writes everywhere; reads remain allowed by default.
        rules.append(
            FilesystemPermission(
                operations=["write"],
                paths=["/**"],
                mode="deny",
            )
        )

    for path in deny_paths or []:
        p = path.strip().replace("\\", "/")
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        rules.append(
            FilesystemPermission(
                operations=["read", "write"],
                paths=[p],
                mode="deny",
            )
        )

    # Sensible defaults when permissions feature is on.
    if enabled and not deny_paths:
        for p in ("/**/.env", "/**/.env.*", "/**/secrets/**"):
            rules.append(
                FilesystemPermission(
                    operations=["read", "write"],
                    paths=[p],
                    mode="deny",
                )
            )

    return rules or None
