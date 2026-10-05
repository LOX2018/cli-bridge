#!/usr/bin/env python3
"""cli-bridge -- thin Hermes tools driving local coding CLIs.

Implements the role contract in the ``multi-cli-orchestration`` skill:

  * ``qoder_run``   -> qoderclicn as the EXECUTOR (writes allowed, worktree-scoped)
  * ``codex_review``-> codex as the READ-ONLY reviewer (code review of a diff)

Design notes (all learned the hard way -- do not "simplify" them away):

  * **stdin is always DEVNULL.** ``codex`` prints
    ``Reading additional input from stdin...`` and BLOCKS waiting for stdin EOF
    when its stdin is an open pipe. Driving it with an open stdin looks like a
    hang. Every subprocess here closes stdin.
  * **Windows .CMD shims are resolved to an absolute path.** ``shutil.which``
    finds the npm ``.CMD`` shim, but ``subprocess.run(["codex", ...])`` cannot
    exec a ``.CMD`` without a shell (WinError 2). We resolve once and invoke the
    absolute path.
  * **stdout may be None** under races; every read is ``(... or "")``.
  * **qoder_run refuses to run without an explicit directory** -- the executor
    must be scoped to an isolated worktree, never an implicit cwd (single-writer
    rule).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
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


def _resolve_shim_target(shim_path: str) -> Optional[List[str]]:
    """Resolve a Windows npm/launcher ``.cmd``/``.bat`` shim to a direct argv prefix.

    Invoking a ``.cmd`` routes argv through cmd.exe, which **truncates multi-line
    prompts** (verified on both opencode and codex: only the first line arrived).
    We recover the real invocation so cmd.exe is bypassed entirely:

      * ``"...\\opencode.exe" %*``     -> ``[<exe>]``
      * ``node "...\\launcher.mjs" %*`` -> ``[<node>, <script>]``

    Returns an argv prefix (list) or None if it cannot be resolved.
    """
    import re
    try:
        content = open(shim_path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    shim_dir = os.path.dirname(shim_path)
    # cmd expands %~dp0 / %dp0% to the shim dir WITH a trailing separator.
    base = shim_dir if shim_dir.endswith(("\\", "/")) else shim_dir + os.sep

    def _abs(raw: str) -> str:
        return os.path.normpath(
            raw.replace("%~dp0", base).replace("%dp0%", base).replace("%~dp0%", base)
        )

    # 1) a real .exe launched directly
    for m in re.finditer(r'"([^"]+\.exe)"', content, re.IGNORECASE):
        cand = _abs(m.group(1))
        if os.path.isfile(cand):
            return [cand]
    # 2) `node "...\foo.mjs"` (launcher script)
    m = re.search(r'\bnode(?:\.exe)?\b\s+"([^"]+\.mjs)"', content, re.IGNORECASE)
    if m:
        script = _abs(m.group(1))
        if os.path.isfile(script):
            node = shutil.which("node") or "node"
            return [node, script]
    return None


def _resolve_bin(name: str) -> Optional[str]:
    """Absolute path to a directly-executable CLI, or None if absent.

    On Windows, resolve npm/launcher ``.cmd`` shims to the REAL invocation so
    cmd.exe never re-parses argv (it truncates multi-line prompts).  Non-Windows
    and plain ``.exe`` paths pass through unchanged.
    """
    if name in _BIN_CACHE:
        return _BIN_CACHE[name]
    found = shutil.which(name)
    if found and os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        target = _resolve_shim_target(found)
        if target:
            _BIN_CACHE[name] = target[0]
            _BIN_CACHE[name + "\x00prefix"] = target  # type: ignore[assignment]
            return target[0]
    _BIN_CACHE[name] = found or None
    return _BIN_CACHE[name]


def _bin_prefix(name: str) -> List[str]:
    """Full argv prefix for *name* (handles ``node launcher.mjs`` shims)."""
    _resolve_bin(name)  # ensure cache populated
    pref = _BIN_CACHE.get(name + "\x00prefix")
    if isinstance(pref, list):
        return list(pref)
    b = _BIN_CACHE.get(name)
    return [b] if b else []


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    for k, v in _HEADLESS_ENV.items():
        env.setdefault(k, v)
    return env


def _clamp_timeout(value: Any) -> int:
    try:
        t = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1, min(t, MAX_TIMEOUT))


def _valid_dir(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    resolved = os.path.realpath(path)
    return resolved if os.path.isdir(resolved) else None


def _run(cmd: List[str], cwd: str, timeout: int) -> Dict[str, Any]:
    """Run a CLI with stdin closed (DEVNULL) and capture output.

    stdin=DEVNULL is the fix for the codex stdin-block hang.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,      # <- never let the CLI block on stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"timed out after {timeout}s", "cmd": cmd}
    except (OSError, ValueError) as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "cmd": cmd}

    out = proc.stdout or ""
    err = proc.stderr or ""
    return {
        "status": "completed" if proc.returncode == 0 else "error",
        "exit_code": proc.returncode,
        "output": out[:MAX_OUTPUT],
        "stderr": err[:MAX_STDERR],
        "cmd": cmd,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

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
    if not prompt:
        return {"status": "error", "error": "prompt is required"}

    cmd = _bin_prefix("qoderclicn") or _bin_prefix("qodercli")
    cmd += ["-p", "--permission-mode", permission_mode, "-w", d]
    if model:
        cmd += ["-m", model]
    cmd += ["--", prompt]
    return _run(cmd, cwd=d, timeout=_clamp_timeout(timeout))


def codex_review(
    directory: str,
    prompt: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    base: Optional[str] = None,
) -> Dict[str, Any]:
    """Run codex's non-interactive code review (READ-ONLY) on a repo diff.

    Defaults to reviewing uncommitted changes (``--uncommitted``); pass ``base``
    (a branch) to review against it instead.
    """
    binary = _resolve_bin("codex")
    if not binary:
        return {"status": "unavailable", "error": "codex not found on PATH"}
    d = _valid_dir(directory)
    if not d:
        return {"status": "error", "error": f"directory not found: {directory!r}"}

    cmd = [binary, "review"]
    # codex review: a custom PROMPT is mutually exclusive with --uncommitted/--base
    # (verified: "the argument '--uncommitted' cannot be used with '[PROMPT]'").
    if prompt:
        cmd += [prompt]
    elif base:
        cmd += ["--base", base]
    else:
        cmd += ["--uncommitted"]
    return _run(cmd, cwd=d, timeout=_clamp_timeout(timeout))


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
        elif action == "status":
            res = {
                "qoderclicn": _resolve_bin("qoderclicn") or _resolve_bin("qodercli"),
                "codex": _resolve_bin("codex"),
            }
        else:
            res = {"error": f"unknown action {action!r}; use: qoder_run, codex_review, codex_exec, status"}
    except Exception as e:  # never let the tool raise into the agent loop
        logger.exception("cli-bridge error")
        res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return json.dumps(res, ensure_ascii=False, default=str)


CLI_BRIDGE_SCHEMA = {
    "name": "cli_bridge",
    "description": (
        "Drive local coding CLIs per the multi-cli-orchestration role contract. "
        "Actions: 'qoder_run' (EXECUTOR -- qoderclicn writes inside an ISOLATED worktree; "
        "directory is mandatory), 'codex_review' (READ-ONLY -- codex reviews a repo diff), "
        "'codex_exec' (READ-ONLY -- codex exec in a read-only sandbox), 'status' (probe availability). "
        "Never point qoder_run at the main working tree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["qoder_run", "codex_review", "codex_exec", "status"],
            },
            "prompt": {"type": "string", "description": "Task / review instructions."},
            "directory": {
                "type": "string",
                "description": "Working directory. For qoder_run this MUST be an isolated worktree.",
            },
            "base": {"type": "string", "description": "codex_review: review against this branch instead of uncommitted."},
            "permission_mode": {"type": "string", "description": "qoder_run permission mode (default dont_ask)."},
            "sandbox": {"type": "string", "description": "codex_exec sandbox (default read-only)."},
            "model": {"type": "string", "description": "Optional model override (qoder_run)."},
            "timeout": {"type": "integer", "description": f"Seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})."},
        },
        "required": ["action"],
    },
}
