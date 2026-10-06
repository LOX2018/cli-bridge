"""JSON schema for the cli_bridge Hermes tool."""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 600
MAX_OUTPUT = 20000
MAX_STDERR = 2000

# Keep local CLIs headless: never pop a browser / auto-share / self-update.
_HEADLESS_ENV = {
    "OPENCODE_DISABLE_EMBEDDED_WEB_UI": "1",
    "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_AUTO_SHARE": "0",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "BROWSER": "true",
    "CI": "1",
}

_BIN_CACHE: Dict[str, Optional[str]] = {}

CLI_BRIDGE_SCHEMA = {
    "name": "cli_bridge",
    "description": (
        "Drive local coding CLIs: opencode, qoderclicn and codex. Write capability is "
        "granted by the worktree boundary, not the CLI name; Hermes is the only "
        "committer. Actions: 'qoder_run' (writes inside an ISOLATED worktree; "
        "directory is mandatory), 'codex_review' (codex reviews a repo diff), "
        "'codex_exec' (codex exec in a read-only sandbox), "
        "'opencode_run' (one-shot opencode task; build agent can write in its --dir), "
        "'opencode_session' (send a message to an existing opencode session), "
        "'opencode_agents' (list agents the local opencode knows), "
        "'opencode_stop' (stop the opencode server this bridge started), "
        "'worktree_create/list/remove' (manage an isolated executor worktree safely), "
        "'status' (probe availability). Never point qoder_run at the main working tree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["qoder_run", "codex_review", "codex_exec",
                         "opencode_run", "opencode_session",
                         "opencode_agents", "opencode_stop",
                         "worktree_create", "worktree_list", "worktree_remove",
                         "status"],
            },
            "prompt": {"type": "string", "description": "Task / review instructions."},
            "directory": {
                "type": "string",
                "description": "Working directory. For qoder_run this MUST be an isolated worktree.",
            },
            "base": {"type": "string", "description": "codex_review: review against this branch instead of uncommitted; worktree_create: start point."},
            "repo": {"type": "string", "description": "worktree_*: the MAIN git repo to attach the worktree to."},
            "branch": {"type": "string", "description": "worktree_create: branch name (default wt/<utc-timestamp>)."},
            "path": {"type": "string", "description": "worktree_create/remove: worktree path (default <repo>-wt-<branch>)."},
            "force": {"type": "boolean", "description": "worktree_remove: force removal (--force). force=True is REFUSED when the worktree contains a junction/symlink; with force=False, links are unlinked (link only, never target) before removal."},
            "permission_mode": {"type": "string", "description": "qoder_run permission mode (default bypass_permissions -- dont_ask denies writes)."},
            "agent": {"type": "string", "description": "opencode_*: agent name (e.g. 'build', 'plan', 'explore'). Leave empty for opencode's default."},
            "variant": {"type": "string", "description": "opencode_run: model variant for reasoning effort (e.g. 'high', 'max')."},
            "model": {"type": "string", "description": "Model override. qoder_run: see qoder docs. opencode_*: e.g. 'opencode/longcat-2.5-preview-free'. Omit for opencode to use its built-in default."},
            "session_id": {"type": "string", "description": "opencode_session: REQUIRED. opencode_run: optionally continue a previous session."},
            "files": {"type": "array", "items": {"type": "string"}, "description": "opencode_run: file paths to attach as context (must exist)."},
            "sandbox": {"type": "string", "description": "codex_exec sandbox (default read-only)."},
            "timeout": {"type": "integer", "description": f"Seconds (default {DEFAULT_TIMEOUT} for CLI actions, 120 for worktree_create/remove, 60 for worktree_list; max {MAX_TIMEOUT})."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}
