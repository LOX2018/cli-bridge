"""Adapter functions that shell out to the three coding CLIs.

    Each is a thin translation layer between cli_bridge tool arguments and one
    CLI's argv. Write access is granted by the worktree boundary (enforced in
    cli_bridge_worktree), never by the CLI name.
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
from cli_bridge_common import (_bin_prefix, _clamp_timeout, _resolve_bin,
                                 _run, _valid_dir)
from cli_bridge_worktree import _assert_isolated_worktree

def qoder_run(
    prompt: str,
    directory: str,
    timeout: int = DEFAULT_TIMEOUT,
    permission_mode: str = "bypass_permissions",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run qoderclicn non-interactively as the executor.

    ``directory`` is REQUIRED and is the isolation boundary: pass an isolated
    git worktree, never the main tree. The prompt contract (worktree-only,
    no git writes) is the caller's responsibility -- this tool enforces only
    the directory boundary.

    ``permission_mode`` defaults to ``bypass_permissions`` so the executor can
    actually WRITE (``dont_ask`` denies any write that would need a prompt --
    verified). Safety comes from the isolated worktree, not from the prompt.
    """
    binary = _resolve_bin("qoderclicn") or _resolve_bin("qodercli")
    if not binary:
        return {"status": "unavailable", "error": "qoderclicn not found on PATH"}
    d = _valid_dir(directory)
    if not d:
        return {"status": "error", "error": f"directory not found: {directory!r} (executor must be worktree-scoped)"}
    # Hard boundary, not a prompt convention: refuse the main working tree
    # before anything runs. A mis-supplied main-tree path (LLM hallucination,
    # lost constraint after context compaction) must not reach
    # bypass_permissions.
    err = _assert_isolated_worktree(d)
    if err:
        return {"status": "error", "error": err, "directory": d}
    if not prompt:
        return {"status": "error", "error": "prompt is required"}

    cmd = _bin_prefix("qoderclicn") or _bin_prefix("qodercli")
    cmd += ["-p", "--permission-mode", permission_mode, "-w", d]
    if model:
        cmd += ["-m", model]
    cmd += ["--", prompt]
    res = _run(cmd, cwd=d, timeout=_clamp_timeout(timeout))
    # Observability, not a control: a caller can audit whether this run was
    # executed with the worktree boundary bypassed. The gate is P0-1's
    # _assert_isolated_worktree above -- a non-bypass mode still needs the
    # worktree, so the annotation only marks what happened, it does not gate.
    if res.get("status") != "error":
        res["permission_mode"] = permission_mode
        res["worktree_scoped"] = permission_mode == "bypass_permissions"
    return res
def codex_review(
    directory: str,
    prompt: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    base: Optional[str] = None,
) -> Dict[str, Any]:
    """Run codex's non-interactive code review on a repo diff.

    Defaults to reviewing uncommitted changes (``--uncommitted``); pass ``base``
    (a branch) to review against it instead.
    """
    binary = _resolve_bin("codex")
    if not binary:
        return {"status": "unavailable", "error": "codex not found on PATH"}
    d = _valid_dir(directory)
    if not d:
        return {"status": "error", "error": f"directory not found: {directory!r}"}

    # _bin_prefix, not _resolve_bin: when codex is a `node launcher.mjs` shim,
    # _resolve_bin returns node itself and `[node, "review", ...]` makes node
    # try to load "review" as a script (exit 1, Cannot find module).
    cmd = _bin_prefix("codex") or [binary]
    cmd += ["review"]
    # codex review: a custom PROMPT is mutually exclusive with --uncommitted/--base
    # ("the argument '--uncommitted' cannot be used with '[PROMPT]'").
    if prompt:
        cmd += [prompt]
    elif base:
        cmd += ["--base", base]
    else:
        cmd += ["--uncommitted"]
    return _run(cmd, cwd=d, timeout=_clamp_timeout(timeout))


# ---------------------------------------------------------------------------
# OpenCode driver (ported from the retired standalone opencode plugin)
# ---------------------------------------------------------------------------
#
# The full OpenCode driver lives in ``opencode_driver.py`` (ported verbatim from
# the retired upstream ``opencode`` plugin so its battle-tested behaviour is
# preserved rather than re-implemented):
#
#   * npm ``.CMD`` shim -> real ``.exe`` resolution (cmd.exe truncates
#     multi-line prompts -- verified on both opencode and codex)
#   * stdin=DEVNULL everywhere (opencode blocks reading an inherited pipe)
#   * JSON event-stream parsing (flat + SDK formats), file diffs, tool results
#   * rate-limit fallback chain + built-in "build" agent fallback
#
# It is imported lazily: importing it starts no subprocess, and its
# ``atexit`` hook only stops a server this driver itself started.
def _opencode_driver():
    """Import the OpenCode driver module, or return None if unavailable."""
    try:
        import opencode_driver as _drv
    except Exception as exc:  # pragma: no cover - import-time safety net
        logger.warning("opencode driver unavailable: %s", exc)
        return None
    return _drv
def _opencode_bin() -> Optional[str]:
    """Absolute path to the opencode binary, or None (for the status probe)."""
    drv = _opencode_driver()
    if drv is None:
        return None
    try:
        return drv._resolve_opencode_bin()
    except Exception:
        return None
def opencode_run(
    prompt: str,
    directory: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    variant: Optional[str] = None,
    session_id: Optional[str] = None,
    files: Optional[List[str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Run a one-shot OpenCode task.

    Default agent is ``build`` (permission ``"*" allow``) so it CAN write — the
    executor/reviewer split comes from the WORKTREE scope, not from the agent.
    ``model=None`` falls back to the driver's ``DEFAULT_OPENCODE_MODEL``
    (NOT opencode's own default, which is rate-limited on the shared free tier).
    """
    drv = _opencode_driver()
    if drv is None:
        return {"status": "unavailable", "error": "opencode driver not importable"}
    if not drv.check_opencode_requirements():
        return {"status": "unavailable", "error": "opencode CLI not found/executable"}
    if not prompt:
        return {"status": "error", "error": "prompt is required"}
    return drv._run_task(
        prompt=prompt,
        directory=directory,
        agent=agent,
        model=model,
        variant=variant,
        session_id=session_id,
        files=files,
        timeout=_clamp_timeout(timeout),
    )
def opencode_session(
    session_id: str,
    prompt: str,
    directory: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Send a multi-turn message to an existing OpenCode session.

    ``model=None`` uses the driver's default (see ``opencode_run``).
    """
    drv = _opencode_driver()
    if drv is None:
        return {"status": "unavailable", "error": "opencode driver not importable"}
    if not drv.check_opencode_requirements():
        return {"status": "unavailable", "error": "opencode CLI not found/executable"}
    if not prompt:
        return {"status": "error", "error": "prompt is required"}
    if not session_id:
        return {"status": "error", "error": "session_id is required"}
    return drv._session_prompt(
        session_id=session_id,
        prompt=prompt,
        directory=directory,
        agent=agent,
        model=model,
        timeout=_clamp_timeout(timeout),
    )
def opencode_agents(timeout: int = 30) -> Dict[str, Any]:
    """Discovery: which agents the local opencode install actually knows."""
    drv = _opencode_driver()
    if drv is None:
        return {"status": "unavailable", "error": "opencode driver not importable"}
    agents = drv._list_agents(timeout=timeout)
    return {
        "status": "completed",
        "opencode_available": drv.check_opencode_requirements(),
        "agents": agents,
        "oh_my_opencode_installed": drv._omo_installed(agents),
        "fallback_agent": drv.FALLBACK_AGENT,
    }
def opencode_stop() -> Dict[str, Any]:
    """Stop the OpenCode server started by this driver (if any)."""
    drv = _opencode_driver()
    if drv is None:
        return {"status": "unavailable", "error": "opencode driver not importable"}
    drv._stop_server()
    return {"status": "stopped"}
def codex_exec(
    prompt: str,
    directory: str,
    timeout: int = DEFAULT_TIMEOUT,
    sandbox: str = "read-only",
) -> Dict[str, Any]:
    """Run ``codex exec`` in a read-only sandbox (general read-only probe/QA)."""
    binary = _resolve_bin("codex")
    if not binary:
        return {"status": "unavailable", "error": "codex not found on PATH"}
    d = _valid_dir(directory)
    if not d:
        return {"status": "error", "error": f"directory not found: {directory!r}"}
    if not prompt:
        return {"status": "error", "error": "prompt is required"}
    cmd = _bin_prefix("codex")
    cmd += ["exec", "-s", sandbox, "--skip-git-repo-check", prompt]
    return _run(cmd, cwd=d, timeout=_clamp_timeout(timeout))


# ---------------------------------------------------------------------------
# Handler + schemas
# ---------------------------------------------------------------------------
