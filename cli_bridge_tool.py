#!/usr/bin/env python3
"""cli-bridge -- thin Hermes tools driving local coding CLIs.

Implements the dispatch contract in the ``multi-cli-orchestration`` skill:

  * ``qoder_run``   -> qoderclicn executor call (writes allowed, worktree-scoped)
  * ``codex_review``-> codex review call (read-only by nature)
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


def _is_git_repo(path: str) -> bool:
    r = _run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"], path, 30)
    return r.get("exit_code") == 0 and (r.get("output") or "").strip() == "true"


def _lstat(p: str):
    """os.lstat with OSError -> None."""
    try:
        return os.lstat(p)
    except OSError:
        return None


def _is_reparse(p: str, st=None) -> bool:
    """True if *p* is a junction / symlink (a link, not a real directory).

    Uses lstat only -- never islink/isdir, which follow the link. Windows
    junctions are reparse points that os.path.islink() reports as False, so
    the reparse attribute is the authoritative test.
    """
    st = st if st is not None else _lstat(p)
    if st is None:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return True
    return os.path.islink(p)


def _iter_reparse_points(root: str):
    """Yield junction/symlink entries under *root*, WITHOUT descending into them.

    os.walk(followlinks=False) does NOT stop at Windows junctions: they are
    reparse points whose os.path.islink() is False, so walk happily descends
    into the junction TARGET. That is how the original code yielded links
    living *inside* a target (e.g. <wt>/node_modules/pkg/.bin/foo) and then
    deleted them -- modifying the target, the exact thing this module exists
    to prevent. So reparse directories are yielded and pruned from dirnames.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            p = os.path.join(dirpath, name)
            if _is_reparse(p):
                yield p
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            p = os.path.join(dirpath, name)
            if _is_reparse(p):
                yield p


def _delete_links_only(root: str) -> List[str]:
    """Delete junction/symlink entries under *root* -- the LINK only, never its target.

    Directories are removed with ``os.rmdir`` (unlinks a junction/dir-symlink
    WITHOUT recursing into the target); files with ``os.remove``. Returns the
    removed link paths for the audit trail.
    """
    removed: List[str] = []
    failed: List[str] = []
    for p in sorted(_iter_reparse_points(root), key=len, reverse=True):
        st = _lstat(p)
        # lstat-based: a directory reparse point (junction) needs rmdir, a
        # file symlink needs remove. isdir()/islink() follow the link and give
        # the wrong answer for broken junctions.
        is_dir_link = bool(st) and stat.S_ISDIR(st.st_mode)
        try:
            if is_dir_link:
                os.rmdir(p)          # junction / dir symlink: removes the link only
            else:
                os.remove(p)         # file symlink
            removed.append(p)
        except OSError as e:
            logger.warning("worktree link cleanup failed for %s: %s", p, e)
            failed.append(p)
    return removed, failed


def _worktree_path_arg(path: str) -> Optional[str]:
    """Validate a worktree path WITHOUT following links (abspath, not realpath)."""
    if not path:
        return None
    p = os.path.abspath(path)
    return p if os.path.isdir(p) else None


def _abandon_worktree(repo: str, path: str, branch: Optional[str] = None) -> Dict[str, Any]:
    """Best-effort teardown of a worktree CREATE left half-done.

    Only ever called on the failure path of worktree_create, on a path we
    just created ourselves. Unlinks junctions first (link only) so we cannot
    recurse into a target, then removes without --force.
    """
    out: Dict[str, Any] = {"path": path}
    try:
        if os.path.isdir(path):
            links = list(_iter_reparse_points(path))
            if links:
                _delete_links_only(path)
                out["unlinked"] = len(links)
    except OSError as e:
        out["unlink_error"] = f"{type(e).__name__}: {e}"
    rm = _run(["git", "-C", repo, "worktree", "remove", path], repo, 60)
    out["remove_exit"] = rm.get("exit_code")
    if rm.get("exit_code") != 0:
        pr = _run(["git", "-C", repo, "worktree", "prune"], repo, 60)
        out["prune_exit"] = pr.get("exit_code")
        out["stderr"] = (rm.get("stderr") or "")[:500]
    if branch:
        bd = _run(["git", "-C", repo, "branch", "-D", branch], repo, 60)
        out["branch_delete_exit"] = bd.get("exit_code")
    return out


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
        return {"status": "error", "error": f"not a git repository: {r!r}"}

    if not branch:
        # Second-resolution collides when two calls land in the same second
        # (verified: second call fails with "target path already exists").
        branch = "wt/" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    if not path:
        safe = branch.replace("/", "-").replace("\\", "-")
        # strip a leading "wt-" so the default path is <repo>-wt-<stamp>,
        # not <repo>-wt-wt-<stamp>
        if safe.startswith("wt-"):
            safe = safe[3:]
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
        # git worktree add already succeeded AND -b already created the branch;
        # leaving them behind orphans the caller (a retry then hits
        # "target path already exists" / "branch already exists").
        cleanup = _abandon_worktree(r, path, branch)
        return {"status": "error",
                "error": f"worktree verification failed (toplevel={top!r})",
                "path": path, "cleanup": cleanup}
    return {"status": "completed", "repo": r, "branch": branch, "path": path}


def worktree_list(repo: str, timeout: int = 60) -> Dict[str, Any]:
    """List worktrees attached to *repo* (audit / cleanup)."""
    r = _valid_dir(repo)
    if not r or not _is_git_repo(r):
        return {"status": "error", "error": f"not a git repository: {repo!r}"}
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
        return {"status": "error", "error": f"not a git repository: {repo!r}"}
    p = _worktree_path_arg(path)
    if not p:
        return {"status": "error", "error": f"worktree path not found: {path!r}"}

    # F6: force must be a real boolean. LLM tool args often arrive as strings
    # and any non-empty string is truthy in Python -- force="false" used to
    # silently upgrade to a forced removal and delete uncommitted work.
    force = _as_bool(force)

    # F4: confirm the path really is a worktree of THIS repo before doing
    # anything destructive. Previously links were deleted first and git only
    # rejected afterwards, so a failed call still left the user's junctions
    # deleted (verified: exit 128 "not a working tree", junction gone).
    top = _run(["git", "-C", p, "rev-parse", "--show-toplevel"], p, 30)
    top_val = (top.get("output") or "").strip()
    if top.get("exit_code") != 0 or not top_val:
        return {"status": "error",
                "error": "path is not inside a git working tree; refusing to touch it",
                "path": p, "stderr": (top.get("stderr") or "")[:300]}
    if os.path.normcase(os.path.realpath(top_val)) != os.path.normcase(os.path.realpath(p)):
        return {"status": "error",
                "error": "path is not the top level of a worktree; refusing to touch it",
                "path": p, "toplevel": top_val}
    main_top = _run(["git", "-C", r, "rev-parse", "--show-toplevel"], r, 30)
    if os.path.normcase(os.path.realpath((main_top.get("output") or "").strip())) \
            == os.path.normcase(os.path.realpath(p)):
        return {"status": "error",
                "error": "path is the MAIN working tree; refusing to remove it", "path": p}

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

    removed_links: List[str] = []
    failed_links: List[str] = []
    if links:
        removed_links, failed_links = _delete_links_only(p)
    # A link we failed to unlink is exactly the thing git --force would
    # follow; do not hand a known-bad tree to git.
    if failed_links:
        return {"status": "error",
                "error": "could not unlink all junction/symlink entries; refusing to remove",
                "removed_links": removed_links, "failed_links": failed_links, "path": p}
    # TOCTOU: the scan is not a guarantee. Re-check right before removing.
    if not force and _iter_reparse_points(p):
        again = list(_iter_reparse_points(p))
        if again:
            return {"status": "error",
                    "error": "junction/symlink appeared during cleanup; refusing to remove",
                    "links": again[:20], "path": p}

    cmd = ["git", "-C", r, "worktree", "remove", p]
    if force:
        cmd.append("--force")
    res = _run(cmd, r, _clamp_timeout(timeout))
    ok = res.get("exit_code") == 0
    # F5: exit 0 is not proof the worktree is gone; verify.
    verified_gone = None
    verified_unregistered = None
    if ok:
        verified_gone = not os.path.exists(p)
        lst = _run(["git", "-C", r, "worktree", "list", "--porcelain"], r, 60)
        listing = lst.get("output") or ""
        verified_unregistered = os.path.normcase(os.path.realpath(p)) not in \
            os.path.normcase(listing)
        if not (verified_gone and verified_unregistered):
            ok = False
    return {
        "status": "completed" if ok else "error",
        "exit_code": res.get("exit_code"),
        "removed_links": removed_links,
        "path": p,
        "verified_path_gone": verified_gone,
        "verified_unregistered": verified_unregistered,
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
    # try to load "review" as a script (verified: exit 1, Cannot find module).
    cmd = _bin_prefix("codex") or [binary]
    cmd += ["review"]
    # codex review: a custom PROMPT is mutually exclusive with --uncommitted/--base
    # (verified: "the argument '--uncommitted' cannot be used with '[PROMPT]'").
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
