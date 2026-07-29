"""Compatibility exports for the settings domain.

New code should import from :mod:`synapse.settings`. This module remains so
existing integrations and extension code do not need an atomic migration.
"""

from synapse.settings.schema import (
    Settings,
    bootstrap_project_env,
    config_search_roots,
    executable_config_dirs,
    find_dotenv,
    load_settings,
    project_config_dir,
    user_config_dir,
)

__all__ = [
    "Settings",
    "bootstrap_project_env",
    "config_search_roots",
    "executable_config_dirs",
    "find_dotenv",
    "load_settings",
    "project_config_dir",
    "user_config_dir",
]
