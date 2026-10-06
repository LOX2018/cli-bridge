"""cli_bridge tool facade.

Re-exports the public surface so ``__init__.py`` and any existing importer keep
working. The real code lives in focused sibling modules, one per responsibility:

    cli_bridge_common    constants, bin resolution, process exec
    cli_bridge_worktree  worktree lifecycle and the isolation gate
    cli_bridge_drivers   opencode / codex / qoderclicn adapters
    cli_bridge_schema    CLI_BRIDGE_SCHEMA
    cli_bridge_handlers  action -> adapter dispatch

``__init__.py`` imports only four of the names below; the rest are re-exported
for the test suite and for backwards compatibility with anything that reached
into this module directly.
"""

from __future__ import annotations

from cli_bridge_common import _resolve_bin
from cli_bridge_drivers import _opencode_bin, qoder_run
from cli_bridge_handlers import cli_bridge_handler
from cli_bridge_schema import CLI_BRIDGE_SCHEMA
from cli_bridge_worktree import _assert_isolated_worktree, worktree_remove

__all__ = [
    "CLI_BRIDGE_SCHEMA",
    "_assert_isolated_worktree",
    "_opencode_bin",
    "_resolve_bin",
    "cli_bridge_handler",
    "qoder_run",
    "worktree_remove",
]
