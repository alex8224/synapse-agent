"""Custom tools for the coding agent."""

from synapse.tools.apply_patch import build_apply_patch_tools
from synapse.tools.describe_image import build_describe_image_tool, build_describe_image_tools
from synapse.tools.filesystem_search import build_filesystem_search_tools
from synapse.tools.session_tools import build_session_tools, build_tool_result_reader_tool

__all__ = [
    "build_apply_patch_tools",
    "build_describe_image_tool",
    "build_describe_image_tools",
    "build_filesystem_search_tools",
    "build_session_tools",
    "build_tool_result_reader_tool",
]
