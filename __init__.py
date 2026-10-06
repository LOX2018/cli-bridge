"""cli-bridge plugin for Hermes Agent.

Registers the ``cli_bridge`` tool, which drives local coding CLIs per the
``multi-cli-orchestration`` dispatch contract. Write capability is granted by
the worktree boundary, not by the CLI name; Hermes is the only committer.

  * ``qoder_run``    -- qoderclicn (worktree-scoped writes)
  * ``codex_review`` -- codex review of a repo diff (read-only by nature)
  * ``codex_exec``   -- codex exec in a read-only sandbox
  * ``opencode_*``   -- opencode (build agent can write inside its --dir)

Lives in ``$HERMES_HOME/plugins/cli-bridge/`` so it survives Hermes updates.
"""

import os
import sys

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from cli_bridge_tool import (  # noqa: E402
    CLI_BRIDGE_SCHEMA,
    _resolve_bin,
    _opencode_bin,
    cli_bridge_handler,
)


def _any_cli_available() -> bool:
    """Tool is offered only when at least one driven CLI is present."""
    return bool(
        _resolve_bin("qoderclicn")
        or _resolve_bin("qodercli")
        or _resolve_bin("codex")
        or _opencode_bin()
    )


def register(ctx):
    """Called by the Hermes plugin system at startup."""
    ctx.register_tool(
        name="cli_bridge",
        toolset="cli-bridge",
        schema=CLI_BRIDGE_SCHEMA,
        handler=cli_bridge_handler,
        check_fn=_any_cli_available,
        requires_env=[],
        is_async=False,
        description=(
            "Drive local coding CLIs: opencode, qoderclicn and codex. Write "
            "capability is granted by the worktree boundary, not the CLI name; "
            "Hermes is the only committer."
        ),
        emoji="\U0001f309",
    )
