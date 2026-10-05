# cli-bridge

**Turn Hermes into the orchestrator of a small CLI team.**

Hermes stops doing everything itself. Instead:

- **`qoderclicn` implements** — writes real code, inside an isolated git worktree.
- **`codex` reviews** — read-only, never touches your files.
- **Hermes plans, dispatches, and writes the final summary** — and nothing else.

The point is not "Hermes can now run a CLI". The point is that **the expensive
work happens somewhere other than your main conversation**.

## Why that matters

Every file an agent reads in your main session stays in that session's context,
forever, and is re-sent on every subsequent turn. Let one agent explore a
codebase, read forty files, and iterate on an implementation, and your main
session is now paying for all of it — on every turn, until the session ends.

With this plugin, that work happens inside `qoderclicn` and `codex`, in their
own processes with their own context. What comes back to Hermes is a result
string, capped at 20 000 characters. Hermes sees the conclusion, not the forty
files.

Two concrete effects:

- **Lower token cost.** The main session stops carrying the entire exploration
  and implementation history.
- **Less context pressure.** Your main conversation stays small enough to keep
  reasoning about the task, instead of drowning in intermediate output.

And each CLI is used for what it is actually good at, rather than one model
being made to do every job.

## The team

| Role | CLI | What it may do | Why this one |
|---|---|---|---|
| **Executor** | `qoderclicn` | Write files — but only inside an isolated worktree | It can act on the repo |
| **Reviewer** | `codex` | Read-only | Independent second opinion on a diff |
| **Probe** | `codex exec` | Read-only, sandboxed | Answer questions about the codebase cheaply |
| **Orchestrator** | Hermes | Dispatch + summarise | Holds the plan and the user's intent |

The executor and the reviewer are deliberately different tools. The agent that
wrote the code is structurally the worst agent to review it — a second,
independent CLI is the whole point.

## Actions

| You want to… | Action | What happens |
|---|---|---|
| Have code written | `qoder_run` | `qoderclicn` edits files inside an isolated worktree |
| Get an independent review | `codex_review` | `codex` reviews; nothing is written |
| Ask a read-only question | `codex_exec` | `codex` inside a read-only sandbox |
| Get a clean directory for the executor | `worktree_create` | new git worktree created, path returned |
| See which worktrees exist | `worktree_list` | audit / cleanup |
| Throw a worktree away | `worktree_remove` | junction-safe removal (see below) |
| Check the plugin is working | `status` | resolved binary paths |

## Requirements

- **Hermes Agent** — this is a plugin, not a standalone tool.
- **Python 3.10+** reachable by Hermes. Pure stdlib; nothing to install.
- **At least one CLI on your `PATH`:**
  - `qoderclicn` (or `qodercli`) — the executor
  - `codex` — the reviewer

If neither is installed, the tool is simply not offered. The absence is silent,
which is why `status` exists.

## Install

```
<hermes-home>/plugins/cli-bridge/
```

Add `cli-bridge` to `plugins.enabled` in your profile's `config.yaml`, then
restart Hermes.

Intended as a **global** plugin — one copy in the shared plugins dir,
junction-linked into each profile — so it survives Hermes updates. A plain copy
into one profile's `plugins/` also works; nothing assumes a junction.

## Usage

```
cli_bridge(action="worktree_create", repo="/path/to/main/repo")

cli_bridge(action="qoder_run",
           directory="<path returned above>",
           prompt="Refactor the parser in foo.py")

cli_bridge(action="codex_review", directory="/path/to/main/repo")

cli_bridge(action="worktree_remove",
           repo="/path/to/main/repo",
           path="<worktree path>")
```

**The one rule `qoder_run` enforces: `directory` is mandatory.** Pass an
isolated worktree, never your main working tree. The plugin checks the path
exists and is a directory — it cannot check that it is *not* your main tree, so
that part is on you.

Finer-grained rules — "write only inside a worktree", "never run git writes",
"don't push" — are a **prompt contract**, not a plugin setting. The plugin
enforces the directory boundary and nothing else.

## Who decides what goes where

The plugin is the **transport**, not the router. It does not decide which CLI
gets a task — Hermes does, following the role contract in the bundled skill:

```
skills/multi-cli-orchestration/SKILL.md
```

That skill defines the dispatch rules: what goes to the executor, what goes to
the reviewer, what Hermes must never delegate (version bumps, commits,
anything needing user consent). Copy it into your profile's skills directory to
have it loaded automatically. The plugin does not read it at runtime — it is
the convention the plugin was built to serve.

Without that skill you still get working transport and a safe write boundary;
you just have to make the dispatch decisions yourself each time.

## Behaviour you should know about

- **All subprocesses close stdin** (`stdin=DEVNULL`). Both `codex` and
  `opencode` block waiting for stdin EOF when stdin is an inherited pipe — zero
  output, no error, looks exactly like a deadlock.
- **`.cmd` / `.bat` shims are resolved to the real executable.** An npm `.cmd`
  shim routes argv through `cmd.exe`, which drops everything after the first
  line, so a multi-line prompt arrives truncated. The plugin finds the real
  `.exe` or `node script.mjs` and invokes that directly.
- **`qoder_run` defaults to `permission_mode=bypass_permissions`.** The stricter
  `dont_ask` mode denies any write needing a prompt, so the executor could not
  write at all. Isolation comes from the worktree, not the permission mode.
- **`worktree_remove` refuses `force` on junctions.** `git worktree remove
  --force` recurses through junctions and symlinks and deletes their *targets*
  — it has emptied a main repo's `node_modules` through a junction. This action
  unlinks them first, **the link only, never the target**, then removes without
  `--force`. Windows-specific; a no-op elsewhere.
- **Headless by default.** `CI=1`, `BROWSER=true`, opencode web UI and
  auto-share disabled. No browser, no self-update, no telemetry.
- **A worktree containing a junction cannot be removed non-forced.** The
  junction itself is an untracked entry, so `git status` reports the tree
  dirty and `force=False` refuses; `force=True` is refused *because* of the
  junction. Delete or `git add` the links first. This deadlock is
  deliberate -- both directions err towards not touching your main tree.
- **Output is capped** at 20 000 chars (2 000 for stderr). Timeouts: 600 s
  default, 3600 s maximum.

## Repository layout

```
plugin.yaml                          manifest
__init__.py                          registers the tool with Hermes
cli_bridge_tool.py                   implementation
skills/multi-cli-orchestration/      bundled role-contract skill
README.md
LICENSE
```

## License

Apache-2.0. See [LICENSE](LICENSE).
