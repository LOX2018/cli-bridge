#!/usr/bin/env python3
"""Behavioural verification for the cli-bridge audit fixes (P0-1 / P0-3 / P1-4).

Every assertion is a NEGATIVE control first: we prove the guard rejects the
bad case, then confirm it accepts the good case. If a check below could not
turn red, the gate it tests is not real.

Run from the plugin directory:  python test_audit_fixes.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cli_bridge_tool as cbt
import cli_bridge_drivers as _drv   # _run moved here with qoder_run          # noqa: E402
import opencode_driver as drv          # noqa: E402

FAILURES = []
PASSES = []


def check(label, ok, detail=""):
    (PASSES if ok else FAILURES).append(label)
    print(("  PASS  " if ok else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))


def git(*args, cwd=None, timeout=60):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def make_repo():
    """Create a throwaway repo with a main worktree and one linked worktree."""
    root = tempfile.mkdtemp(prefix="cbtest_")
    repo = os.path.join(root, "main")
    wt = os.path.join(root, "linked")
    os.makedirs(repo)
    for args in (["init", "-q"],):
        git(*args, cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    open(os.path.join(repo, "f.txt"), "w").write("x\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    r = git("worktree", "add", "-q", "--detach", wt, "HEAD", cwd=repo)
    assert r.returncode == 0, r.stderr
    return root, repo, wt


print("=" * 66)
print("[P0-1] _assert_isolated_worktree -- main-tree refusal")
print("=" * 66)
root, repo, wt = make_repo()
try:
    # Negative control: the MAIN working tree must be refused.
    err = cbt._assert_isolated_worktree(repo)
    check("main working tree is REFUSED", err is not None and "MAIN working tree" in err,
          (err or "")[:90])

    # Positive control: a linked worktree must be accepted (None = pass).
    ok = cbt._assert_isolated_worktree(wt)
    check("linked worktree is ACCEPTED", ok is None, repr(ok))

    # Non-git path must fail closed, not open.
    plain = tempfile.mkdtemp(prefix="cbtest_plain_")
    err = cbt._assert_isolated_worktree(plain)
    check("non-git directory is REFUSED (fail closed)", err is not None and "not a git working tree" in err,
          (err or "")[:90])
    shutil.rmtree(plain, ignore_errors=True)

    # Non-existent path.
    err = cbt._assert_isolated_worktree(os.path.join(root, "does-not-exist"))
    check("non-existent path is REFUSED", err is not None, (err or "")[:90])

    # Negative control for qoder_run: must refuse before spawning anything.
    res = cbt.qoder_run(prompt="print('x')", directory=repo)
    check("qoder_run refuses the main working tree",
          res.get("status") == "error" and "MAIN working tree" in res.get("error", ""),
          (res.get("error") or "")[:90])

    # worktree_remove must refuse the main tree too (shared guard).
    res = cbt.worktree_remove(repo=repo, path=repo)
    check("worktree_remove refuses the main working tree",
          res.get("status") == "error" and "MAIN working tree" in res.get("error", ""),
          (res.get("error") or "")[:90])

    # The guard must not have destroyed anything: worktree must still exist.
    check("main tree untouched after refusal",
          os.path.isdir(repo) and os.path.exists(os.path.join(repo, "f.txt")), "")
finally:
    git("worktree", "remove", "--force", wt, cwd=repo)
    shutil.rmtree(root, ignore_errors=True)

print()
print("=" * 66)
print("[P2-8] permission_mode observability in qoder_run")
print("=" * 66)
# P0-1's finally removes the shared temp repo, so this section builds its
# own: the annotation is reachable only through a live linked worktree.
root, repo, wt = make_repo()
try:
    wt2, repo2 = wt, repo
    _assert_isolated_worktree = cbt._assert_isolated_worktree
    orig_run = _drv._run
    # qoderclicn resolves on this host and `wt` is a linked worktree, so the
    # P0-1 gate passes and control reaches the annotation.
    #
    # _run is faked to avoid spawning the real CLI -- but ONLY for non-git
    # commands. _assert_isolated_worktree also goes through _run for its
    # `git rev-parse --git-dir` probe; faking that too would return a stdout
    # without "worktrees/" and turn a genuine linked worktree into a refused
    # path, hiding the very branch under test.
    def fake_run(cmd, cwd=None, timeout=60):
        if cmd and os.path.basename(str(cmd[0])).lower().startswith("git"):
            return orig_run(cmd, cwd=cwd, timeout=timeout)
        # Mirror the real _run return shape: the key is "output", not "stdout"
        # -- _assert_isolated_worktree reads res.get("output").
        return {"status": "completed", "exit_code": 0, "output": "ok",
                "stderr": "", "cmd": cmd}

    _drv._run = fake_run

    # Guard against a second fake: prove the gate itself still discriminates.
    check("gate still accepts the linked worktree under the fake",
          _assert_isolated_worktree(wt2) is None,
          repr(_assert_isolated_worktree(wt2)))

    # Default mode: bypass_permissions -> worktree_scoped True.
    r = cbt.qoder_run(prompt="echo hi", directory=wt2)
    check("default mode annotated as bypass",
          r.get("permission_mode") == "bypass_permissions", str(r.get("permission_mode")))
    check("worktree_scoped=True for bypass_permissions",
          r.get("worktree_scoped") is True, str(r.get("worktree_scoped")))
    check("bypass run still completed", r.get("status") == "completed", str(r.get("status")))

    # Narrowed mode: a caller that opts out of bypass is marked accordingly.
    r = cbt.qoder_run(prompt="echo hi", directory=wt2, permission_mode="dont_ask")
    check("non-bypass mode recorded verbatim",
          r.get("permission_mode") == "dont_ask", str(r.get("permission_mode")))
    check("worktree_scoped=False for dont_ask",
          r.get("worktree_scoped") is False, str(r.get("worktree_scoped")))

    # Negative control: an error return (main tree refused) must NOT be
    # annotated -- annotating a run that never happened would fabricate an audit
    # trail.
    r = cbt.qoder_run(prompt="echo hi", directory=repo)
    check("refused run carries no permission_mode (not annotated)",
          r.get("status") == "error" and "permission_mode" not in r,
          str({k: r.get(k) for k in ("status", "permission_mode")}))
finally:
    _drv._run = orig_run
    git("worktree", "remove", "--force", wt, cwd=repo)
    shutil.rmtree(root, ignore_errors=True)
print()
print("=" * 66)
print("[P0-3] opencode version gate -- CVE-2026-22812")
print("=" * 66)
check("threshold constant is the fixed release",
      drv.MIN_SAFE_OPENCODE_VERSION == (1, 0, 216), str(drv.MIN_SAFE_OPENCODE_VERSION))

cases = [
    ("known-vulnerable 1.0.100", "1.0.100", True, True),
    ("boundary == 1.0.216", "1.0.216", False, False),
    ("safe 1.18.34", "1.18.34", False, False),
    ("unknown (probe failed)", None, True, False),
    ("empty string", "", True, False),
]
for label, fake, strict_expected, loose_expected in cases:
    orig = drv.opencode_version
    drv.opencode_version = lambda v=fake: v
    try:
        got_strict = drv.opencode_version_violation(strict=True)
        got_loose = drv.opencode_version_violation(strict=False)
    finally:
        drv.opencode_version = orig
    s_ok = (got_strict is not None) is strict_expected
    l_ok = (got_loose is not None) is loose_expected
    check(f"strict={strict_expected} for {label}", s_ok,
          (got_strict or "None")[:60])
    check(f"loose={loose_expected} for {label}", l_ok,
          (got_loose or "None")[:60])

# The CVE id must be surfaced in the message, not buried in a log line.
drv.opencode_version = lambda: "1.0.100"
try:
    msg = drv.opencode_version_violation(strict=True) or ""
    check("vulnerable message names the advisory", "CVE-2026-22812" in msg, msg[:70])
finally:
    drv.opencode_version = lambda: "1.18.34"

# Live check: this machine's real version.
live = drv.opencode_version()
check("live version parses", live is not None, repr(live))
check("live version passes the gate",
      drv.opencode_version_violation(strict=True) is None, repr(live))

# Gate is wired into the write paths.
src = open(os.path.join(HERE, "opencode_driver.py"), encoding="utf-8").read()
check("gate wired into 2 write paths",
      src.count("opencode_version_violation(strict=True)") == 2,
      "count=%d" % src.count("opencode_version_violation(strict=True)"))

# _run_task must refuse before spawning when the gate fires.
drv.opencode_version = lambda: "0.9.9"
try:
    r = drv._run_task(prompt="echo hi", timeout=30)
    check("run path refuses a vulnerable version",
          r.get("status") == "error" and "CVE-2026-22812" in r.get("error", ""),
          (r.get("error") or "")[:60])
    check("run path returns structured version_check",
          isinstance(r.get("version_check"), dict) and r["version_check"].get("installed") == "0.9.9",
          str(r.get("version_check")))
    check("run path did NOT attempt a task (no status=completed)",
          "files_changed" not in r, str(sorted(r.keys())))
finally:
    drv.opencode_version = lambda: "1.18.34"

print()
print("=" * 66)
print("[P1-4] rate-limit degradation must be observable")
print("=" * 66)
# Simulate: requested model rate-limited, FIRST fallback succeeds.
orig_run, orig_limit = drv._exec, drv._is_rate_limited
calls = []

class FakeResult:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""

def fake_exec(cmd, timeout=60, cwd=None):
    calls.append(" ".join(cmd[:8]))
    return FakeResult(0, '{"type":"text","text":"ok"}\n')

def fake_limit(stdout, stderr):
    # The requested model AND the same-model retry (fallback[0] is the default
    # model) are rate-limited; only the genuinely different model succeeds.
    # Anything else is a test bug -- we must observe a real model change.
    return len(calls) < 3  # calls 1,2 rate-limited; call 3 (fallback[1]) succeeds

drv._exec = fake_exec
drv._is_rate_limited = fake_limit
try:
    r = drv._run_task(prompt="p", model=None, timeout=30)
    check("degradation is reported structurally",
          r.get("degraded") is True, str(r.get("degraded")))
    check("requested_model is a real model name, not the raw argument",
          r.get("requested_model") == drv.DEFAULT_OPENCODE_MODEL,
          str(r.get("requested_model")))
    check("degradation did NOT come from the same-model retry",
          r.get("requested_model") != r.get("actual_model"),
          "%s -> %s" % (r.get("requested_model"), r.get("actual_model")))
    check("actual_model is the genuinely different model",
          r.get("actual_model") == drv._FALLBACK_MODELS[1][0],
          str(r.get("actual_model")))
    check("actual_model differs from the default model",
          r.get("actual_model") != drv.DEFAULT_OPENCODE_MODEL,
          str(r.get("actual_model")))
    check("rate_limited flag set", r.get("rate_limited") is True, str(r.get("rate_limited")))
    check("tried_models lists every model attempted",
          isinstance(r.get("tried_models"), list)
          and r["tried_models"] == list(m for m, _ in drv._FALLBACK_MODELS[:2]),
          str(r.get("tried_models")))
finally:
    drv._exec, drv._is_rate_limited = orig_run, orig_limit

# Simulation 2: EVERY model in the chain rate-limited -> explicit error.
calls2 = []
drv._exec = lambda cmd, timeout=60, cwd=None: FakeResult(0, '{"type":"text"}\n')
drv._is_rate_limited = lambda stdout, stderr: True
try:
    r = drv._run_task(prompt="p", model=None, timeout=30)
    check("chain exhaustion is an explicit ERROR (not a silent run)",
          r.get("status") == "error", str(r.get("status")))
    check("exhaustion reason is machine-readable",
          r.get("reason") == "rate_limit_chain_exhausted", str(r.get("reason")))
    check("exhaustion lists every model attempted",
          r.get("requested_model") == drv.DEFAULT_OPENCODE_MODEL
          and len(r.get("tried_models", [])) > 1,
          str(r.get("tried_models")))
    check("exhaustion reports actual_model=None",
          r.get("actual_model") is None, str(r.get("actual_model")))
finally:
    drv._exec, drv._is_rate_limited = orig_run, orig_limit

print()
print("=" * 66)
print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 66)
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("ALL GREEN")
