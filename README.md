# cli-bridge

**Global, project-agnostic Hermes plugin.** Drives local coding CLIs and exposes
them to Hermes as the `cli_bridge` tool.

| | |
|---|---|
| Install location | `$HERMES_HOME/plugins/cli-bridge/` (the **global** plugins dir = default profile) |
| Profile wiring | junction-linked into each profile's `plugins/` (same model as the `opencode` plugin) |
| Enabled in | `default`, `lox`, `wx-llm` (each profile's `config.yaml` → `plugins.enabled`) |
| Tool | `cli_bridge` |

## Actions

| action | CLI | Mode | Notes |
|---|---|---|---|
| `qoder_run` | `qoderclicn` | **executor** (writes) | `directory` is **required** = the isolation boundary. Pass an isolated git worktree, never the main tree. |
| `codex_review` | `codex review` | read-only | Review a repo diff. |
| `codex_exec` | `codex exec` | read-only | General read-only probe/QA in a read-only sandbox. |
| `status` | — | — | Report resolved binary paths. |

## Why it exists (three Windows bugs it neutralizes)

1. **stdin must be closed.** `codex` and `opencode` both block reading an
   inherited stdin pipe (Electron/agent processes give them one) → the call hangs
   with zero output. Every subprocess here uses `stdin=DEVNULL`.
2. **`.CMD` shims truncate multi-line prompts.** npm/launcher `.cmd` wrappers
   route argv through `cmd.exe`, which **drops everything after the first line**.
   This plugin resolves the shim to the real invocation (`"...\x.exe"` or
   `node "...\launcher.mjs"`) so `cmd.exe` never re-parses argv.
3. **`stdout` may be `None`** under races — every read is `(... or "")`.

Plus: `qoder_run` defaults `permission_mode=bypass_permissions` (safety comes from
the isolated worktree, not the prompt), and `codex_review` respects the
`--uncommitted` / PROMPT mutual exclusion.

## Role contract

See the `multi-cli-orchestration` skill for the full role table (who writes, who
reviews, who only reads, single-writer rule).
