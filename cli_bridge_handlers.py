"""Dispatch from the cli_bridge tool's ``action`` field to the adapters.

    Keeps argument coercion in one place so handlers are testable without a
    live CLI.
    """
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
from cli_bridge_common import _as_bool, _resolve_bin
from cli_bridge_drivers import (_opencode_bin, codex_exec, codex_review,
                                opencode_agents, opencode_run,
                                opencode_session, opencode_stop,
                                qoder_run)
from cli_bridge_schema import CLI_BRIDGE_SCHEMA
from cli_bridge_worktree import worktree_create, worktree_list, worktree_remove

def cli_bridge_handler(args: Dict[str, Any], **kwargs) -> str:
    action = str(args.get("action", "") or "")
    try:
        if action == "qoder_run":
            res = qoder_run(
                prompt=str(args.get("prompt", "") or ""),
                directory=str(args.get("directory", "") or ""),
                timeout=args.get("timeout", DEFAULT_TIMEOUT),
                permission_mode=str(args.get("permission_mode", "bypass_permissions") or "bypass_permissions"),
                model=(str(args["model"]) if args.get("model") else None),
            )
        elif action == "codex_review":
            res = codex_review(
                directory=str(args.get("directory", "") or ""),
                prompt=(str(args["prompt"]) if args.get("prompt") else None),
                timeout=args.get("timeout", DEFAULT_TIMEOUT),
                base=(str(args["base"]) if args.get("base") else None),
            )
        elif action == "codex_exec":
            res = codex_exec(
                prompt=str(args.get("prompt", "") or ""),
                directory=str(args.get("directory", "") or ""),
                timeout=args.get("timeout", DEFAULT_TIMEOUT),
                sandbox=str(args.get("sandbox", "read-only") or "read-only"),
            )
        elif action == "worktree_create":
            res = worktree_create(
                repo=str(args.get("repo", "") or args.get("directory", "") or ""),
                branch=(str(args["branch"]) if args.get("branch") else None),
                path=(str(args["path"]) if args.get("path") else None),
                base=(str(args["base"]) if args.get("base") else None),
                timeout=args.get("timeout", 120),
            )
        elif action == "worktree_list":
            res = worktree_list(
                repo=str(args.get("repo", "") or args.get("directory", "") or ""),
                timeout=args.get("timeout", 60),
            )
        elif action == "worktree_remove":
            res = worktree_remove(
                repo=str(args.get("repo", "") or args.get("directory", "") or ""),
                path=str(args.get("path", "") or ""),
                force=_as_bool(args.get("force", False)),
                timeout=args.get("timeout", 120),
            )
        elif action == "opencode_run":
            res = opencode_run(
                prompt=str(args.get("prompt", "") or ""),
                directory=(str(args["directory"]) if args.get("directory") else None),
                agent=(str(args["agent"]) if args.get("agent") else None),
                model=(str(args["model"]) if args.get("model") else None),
                variant=(str(args["variant"]) if args.get("variant") else None),
                session_id=(str(args["session_id"]) if args.get("session_id") else None),
                files=(list(args["files"]) if args.get("files") else None),
                timeout=args.get("timeout", DEFAULT_TIMEOUT),
            )
        elif action == "opencode_session":
            res = opencode_session(
                session_id=str(args.get("session_id", "") or ""),
                prompt=str(args.get("prompt", "") or ""),
                directory=(str(args["directory"]) if args.get("directory") else None),
                agent=(str(args["agent"]) if args.get("agent") else None),
                model=(str(args["model"]) if args.get("model") else None),
                timeout=args.get("timeout", DEFAULT_TIMEOUT),
            )
        elif action == "opencode_agents":
            res = opencode_agents(timeout=args.get("timeout", 30))
        elif action == "opencode_stop":
            res = opencode_stop()
        elif action == "status":
            res = {
                "status": "completed",
                "qoderclicn": _resolve_bin("qoderclicn") or _resolve_bin("qodercli"),
                "codex": _resolve_bin("codex"),
                "opencode": _opencode_bin(),
            }
        else:
            res = {"error": "unknown action %r; use: %s" % (
                action,
                ", ".join(CLI_BRIDGE_SCHEMA["parameters"]["properties"]["action"]["enum"]))}
    except Exception as e:  # never let the tool raise into the agent loop
        logger.exception("cli-bridge error")
        res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return json.dumps(res, ensure_ascii=False, default=str)
