"""cli-bridge plugin for Hermes Agent.

Registers the ``cli_bridge`` tool, which drives local coding CLIs according to
the ``multi-cli-orchestration`` role contract:

  * ``qoder_run``    -- qoderclicn as EXECUTOR (worktree-scoped writes)
  * ``codex_review`` -- codex as READ-ONLY reviewer
  * ``codex_exec``   -- codex exec in a read-only sandbox

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
    cli_bridge_handler,
)


def _any_cli_available() -> bool:
    """Tool is offered only when at least one driven CLI is present."""
    return bool(_resolve_bin("qoderclicn") or _resolve_bin("qodercli") or _resolve_bin("codex"))


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
            "Drive local coding CLIs (qoderclicn executor / codex read-only reviewer) "
            "per the multi-cli-orchestration role contract."
        ),
        emoji="\U0001f309",
    )
