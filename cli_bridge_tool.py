#!/usr/bin/env python3
"""cli-bridge -- thin Hermes tools driving local coding CLIs.

Implements the role contract in the ``multi-cli-orchestration`` skill:

  * ``qoder_run``   -> qoderclicn as the EXECUTOR (writes allowed, worktree-scoped)
  * ``codex_review``-> codex as the READ-ONLY reviewer (code review of a diff)
  * ``worktree_*``  -> create / list / remove an ISOLATED git worktree for the
    executor, so the caller never hand-manages the write scope.

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
import stat
import subprocess
import time
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


def _is_git_repo(path: str) -> bool:
    r = _run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"], path, 30)
    return r.get("exit_code") == 0 and (r.get("output") or "").strip() == "true"


def _iter_reparse_points(root: str):
    """Yield paths of junction/symlink entries under *root* (never followed)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if (attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) or os.path.islink(p):
                yield p


def _delete_links_only(root: str) -> List[str]:
    """Delete junction/symlink entries under *root* -- the LINK only, never its target.

    Directories are removed with ``os.rmdir`` (unlinks a junction/dir-symlink
    WITHOUT recursing into the target); files with ``os.remove``. Returns the
    removed link paths for the audit trail.
    """
    removed: List[str] = []
    for p in sorted(_iter_reparse_points(root), key=len, reverse=True):
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)          # junction / dir symlink: removes the link only
            else:
                os.remove(p)         # file symlink
            removed.append(p)
        except OSError as e:
            logger.warning("worktree link cleanup failed for %s: %s", p, e)
    return removed


def _worktree_path_arg(path: str) -> Optional[str]:
    """Validate a worktree path WITHOUT following links (abspath, not realpath)."""
    if not path:
        return None
    p = os.path.abspath(path)
    return p if os.path.isdir(p) else None


def worktree_create(
    repo: str,
    branch: Optional[str] = None,
    path: Optional[str] = None,
    base: Optional[str] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Create an ISOLATED git worktree for the executor; return its path.

    ``branch`` defaults to ``wt/<utc-timestamp>``; ``path`` defaults to a sibling
    directory ``<repo>-wt-<sanitized-branch>``. ``base`` is an optional start
    point (branch/commit) for ``git worktree add -b``.
    """
    r = _valid_dir(repo)
    if not r:
        return {"status": "error", "error": f"repo not found: {repo!r}"}
    if not _is_git_repo(r):
        return {"status": "error", "error": f"not a git worktree: {r!r}"}

    if not branch:
        branch = "wt/" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    if not path:
        safe = branch.replace("/", "-").replace("\\", "-")
        path = os.path.join(os.path.dirname(r), os.path.basename(r) + "-wt-" + safe)
    path = os.path.abspath(path)
    if os.path.exists(path):
        return {"status": "error", "error": f"target path already exists: {path!r}"}

    cmd = ["git", "-C", r, "worktree", "add", "-b", branch, path]
    if base:
        cmd.append(base)
    res = _run(cmd, r, _clamp_timeout(timeout))
    if res.get("exit_code") != 0:
        return {"status": "error", "error": "git worktree add failed", "cmd": cmd,
                "stderr": res.get("stderr"), "output": res.get("output")}

    chk = _run(["git", "-C", path, "rev-parse", "--show-toplevel"], path, 30)
    top = (chk.get("output") or "").strip()
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(path)):
        return {"status": "error", "error": f"worktree verification failed (toplevel={top!r})", "path": path}
    return {"status": "completed", "repo": r, "branch": branch, "path": path}


def worktree_list(repo: str, timeout: int = 60) -> Dict[str, Any]:
    """List worktrees attached to *repo* (audit / cleanup)."""
    r = _valid_dir(repo)
    if not r or not _is_git_repo(r):
        return {"status": "error", "error": f"not a git worktree: {repo!r}"}
    res = _run(["git", "-C", r, "worktree", "list", "--porcelain"], r, _clamp_timeout(timeout))
    if res.get("exit_code") != 0:
        return {"status": "error", "error": "git worktree list failed", "stderr": res.get("stderr")}
    return {"status": "completed", "repo": r, "worktrees": res.get("output") or ""}


def worktree_remove(
    repo: str,
    path: str,
    force: bool = False,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Remove a worktree SAFELY (link-aware).

    Refuses ``force`` if the worktree contains junction/symlink; unlinks those
    links FIRST (link only, never target), then runs ``git worktree remove``
    WITHOUT recursing through links.
    """
    r = _valid_dir(repo)
    if not r or not _is_git_repo(r):
        return {"status": "error", "error": f"not a git worktree: {repo!r}"}
    p = _worktree_path_arg(path)
    if not p:
        return {"status": "error", "error": f"worktree path not found: {path!r}"}

    links = list(_iter_reparse_points(p))
    if links and force:
        return {"status": "error",
                "error": "refusing --force: worktree contains junction/symlink (would recurse into targets)",
                "links": links[:20], "count": len(links)}
    if not force:
        st = _run(["git", "-C", p, "status", "--porcelain"], p, 60)
        if (st.get("output") or "").strip():
            return {"status": "error",
                    "error": "worktree has uncommitted changes; commit them or pass force=true",
                    "dirty": st.get("output")}

    removed_links = _delete_links_only(p) if links else []
    cmd = ["git", "-C", r, "worktree", "remove", p]
    if force:
        cmd.append("--force")
    res = _run(cmd, r, _clamp_timeout(timeout))
    ok = res.get("exit_code") == 0
    return {
        "status": "completed" if ok else "error",
        "exit_code": res.get("exit_code"),
        "removed_links": removed_links,
        "stderr": res.get("stderr"),
        "output": res.get("output"),
    }


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
                force=bool(args.get("force", False)),
                timeout=args.get("timeout", 120),
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
        "'codex_exec' (READ-ONLY -- codex exec in a read-only sandbox), "
        "'worktree_create/list/remove' (manage an isolated executor worktree safely), "
        "'status' (probe availability). Never point qoder_run at the main working tree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["qoder_run", "codex_review", "codex_exec",
                         "worktree_create", "worktree_list", "worktree_remove", "status"],
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
            "force": {"type": "boolean", "description": "worktree_remove: force removal; refused if junction/symlink present."},
            "permission_mode": {"type": "string", "description": "qoder_run permission mode (default bypass_permissions -- dont_ask denies writes)."},
            "sandbox": {"type": "string", "description": "codex_exec sandbox (default read-only)."},
            "model": {"type": "string", "description": "Optional model override (qoder_run)."},
            "timeout": {"type": "integer", "description": f"Seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})."},
        },
        "required": ["action"],
    },
}
