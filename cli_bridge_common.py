"""Common plumbing shared by every cli-bridge module.

    Timeout/output constants, the headless env applied to every child process,
    Windows .cmd shim resolution, bin discovery and type coercion.

    ``_run`` is the ONLY place that spawns a process; keep it that way so the
    stdin=DEVNULL / truncation / timeout policy stays in one auditable spot.
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
def _as_bool(value: Any) -> bool:
    """Coerce a bool-ish argument to a real bool.

    Tool arguments frequently arrive as strings, and in Python any non-empty
    string is truthy -- force="false" would silently become a forced removal.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)
def _valid_dir(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    resolved = os.path.realpath(path)
    return resolved if os.path.isdir(resolved) else None


# ---------------------------------------------------------------------------
# Worktree lifecycle (T-4) -- isolated write scope for the executor
# ---------------------------------------------------------------------------
# The executor (qoder_run) may only write inside an isolated git worktree (the
# single-writer rule). These primitives automate create / list / remove so the
# caller never hand-manages a worktree.
#
# SAFETY (learned the hard way -- see the multi-cli-orchestration skill):
#   ``git worktree remove --force`` recurses into the worktree and FOLLOWS
#   junction/symlink targets, which can empty the MAIN tree (a real incident
#   deleted the main repo's node_modules through a junction). We therefore:
#     1. refuse ``force`` when the worktree tree contains any reparse point
#        (junction / symlink), and
#     2. delete such links FIRST -- the LINK only, never its target -- before any
#        ``git worktree remove``.
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
