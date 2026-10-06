"""Git worktree lifecycle with junction-safe removal.

    ``_assert_isolated_worktree`` is the isolation gate shared by the
    qoder_run execution entry and worktree_remove. The discriminator is
    whether ``git rev-parse --git-dir`` contains "worktrees/": the main
    working tree reports a plain ".git", while a linked worktree reports an
    absolute path under worktrees/. Non-git and non-existent paths fail
    closed rather than falling through.
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
from cli_bridge_common import _as_bool, _clamp_timeout, _run, _valid_dir

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
def _assert_isolated_worktree(path: str, timeout: int = 30) -> Optional[str]:
    """Refuse *path* unless it is a linked worktree (never the main tree).

    Returns an error message, or ``None`` when the path passes.

    The discriminator is ``git rev-parse --git-dir``: for the MAIN working
    tree git reports ``.git``; for every linked worktree it reports
    ``<common>/.git/worktrees/<name>``. That substring test needs no repo
    argument, so it works for ``qoder_run`` which receives a bare directory
    and has no repository reference to compare against.

    A ``--git-common-dir`` comparison cannot work here: with no repo argument
    the main tree returns the relative ``.git`` while a linked worktree returns
    an absolute path, so the two can never compare equal.

    Non-git paths and git failures fail CLOSED: an executor must prove its
    scope is isolated, and a path we cannot classify is not accepted.
    """
    p = _worktree_path_arg(path)
    if not p:
        return f"not a directory: {path!r}"
    if not _is_git_repo(p):
        return (f"not a git working tree: {path!r} (executor must run inside an "
                f"isolated git worktree, not the main tree)")
    res = _run(["git", "-C", p, "rev-parse", "--git-dir"], p, timeout)
    gd = (res.get("output") or "").strip()
    if res.get("exit_code") != 0 or not gd:
        return f"could not read git-dir of {path!r}; refusing to run there"
    if "worktrees" in gd.replace(os.sep, "/"):
        return None  # linked worktree -- isolated
    return (f"refusing to run: {path!r} is the MAIN working tree (git-dir={gd!r}). "
            f"Create an isolated worktree with worktree_create and pass that path.")
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
        # (a second call fails with "target path already exists").
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

    # force must be a real boolean: LLM tool args often arrive as strings and
    # any non-empty string is truthy in Python, so force="false" would silently
    # upgrade to a forced removal and delete uncommitted work.
    force = _as_bool(force)

    # Confirm the path really is a worktree of THIS repo before doing anything
    # destructive. Links are unlinked only after this check, so a failed call
    # cannot leave junctions behind.
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
    # Shared main-tree guard (same rule as the executor entry point).
    err = _assert_isolated_worktree(p)
    if err:
        return {"status": "error", "error": err, "path": p}

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
    # exit 0 is not proof the worktree is gone; verify.
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
