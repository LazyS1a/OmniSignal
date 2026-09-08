"""Optional, process-isolated Pluggy processing SDK."""

from .contracts import (
    PluginInputRecord,
    PluginManifest,
    PluginOutputRecord,
    PluginResult,
    load_plugin_manifest,
)
from .hooks import hookimpl, hookspec
from .runner import PluginExecution, run_plugin
from .persistence import persist_plugin_execution

__all__ = [
    "PluginExecution",
    "PluginInputRecord",
    "PluginManifest",
    "PluginOutputRecord",
    "PluginResult",
    "hookimpl",
    "hookspec",
    "load_plugin_manifest",
    "persist_plugin_execution",
    "run_plugin",
]
