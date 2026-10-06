---
name: multi-cli-orchestration
description: "Use when 把本机多个编码 CLI（opencode/qoderclicn/codex 等）纳入 Hermes 统一编排：角色分配·自动分派·隔离写权·独立验收。"
version: 2.0.0
author: LOX (通过对话提炼)
license: Proprietary
platforms: [windows]
metadata:
  hermes:
    tags: [orchestration, delegation, multi-cli, opencode, qoderclicn, codex, role-assignment, worktree-isolation, verification, sole-writer]
    related_skills: [external-agent-delegation, opencode-usage, multi-session-collaboration, subagent-delegation, mechanical-gate-verification, capability-delivery-verification]
---

# 多 CLI 统一编排（Hermes 为唯一入口）

> 适用：本机装有多个编码 CLI，**Hermes 作为唯一入口**按任务性质分派给对应 CLI，
> 监督其工作并**独立验收**其成果。
>
> 与 `external-agent-delegation` 的分工：那份管 **Hermes ↔ 单个外部代理** 的委派边界；
> 本份管 **多 CLI 的角色编排**（谁写、谁审、谁只读、谁独占合并）。

## 一、角色契约（唯一权威表）

写权按**任务**分派：三个 CLI **都是执行者**，写权由 **worktree 边界**授予，
**不由「该代理是否被指定为只读」授予**。

| 角色 | 承担者 | 权限边界 |
|---|---|---|
| **唯一入口 / 仲裁者 / 唯一提交者** | **Hermes** | 写主工作树·升版·提交·部署·记忆·回复 |
| **编码子代理（可并行，任一 CLI）** | **opencode / codex / qoderclicn** | **独占 worktree 内可写**；主树对子代理只读 |
| **复核者（须与执行者分离）** | 任一**未参与该任务实现**的 CLI | **只读**复核 diff |
| **并行只读取证**（可选） | Hermes `delegate_task` | **只读**；**禁止冒充外部代理** |

### 三条不可协商的分离律

1. **写权来自 worktree 边界，不来自代理身份**：三个 CLI 都可执行。安全靠**独占
   worktree**，不靠「只给一个代理写权」。
2. **执行者 ≠ 复核者**：谁实现，谁不得复核自己的实现。复核必须由**另一个** CLI 承担。
3. **子代理永不允许 git 写操作**：`add` / `commit` / `push` / `checkout` / `reset` / `stash`
   一律禁止；所有提交由 Hermes 独占。

### 并发写权隔离（并行执行的唯一安全前提）

N 路并行执行 = N 个**互不相交**的 worktree。**同一棵源码树不得有两个写者**
（last-writer-wins 静默丢改动，无冲突提示，见 `multi-session-collaboration`）。
隔离原语必须 **junction 安全**：删 worktree 前先解链，**绝不 `--force` 递归含链接的树**。

## 二、自动分派决策树

```
任务到达
├─ 产出是「结果」（报告/方案/diff/评审/上游情报）？ ──► 只读路径
│   ├─ 代码审计/架构/探索/调研/知识库/依赖分析 ──► 任一 CLI 只读跑
│   ├─ 需要「改动的评审」或第二意见 ──────────► 另一个 CLI 只读复核
│   └─ 独立交叉对账 ──────────────────────────► Hermes delegate_task（只读）
│
├─ 产出是「可运行的代码改动」？ ──► 执行路径
│   ├─ 单点精准修改（已知确切改法）──────────► Hermes 自己改（不派发）
│   ├─ 多文件/需探索的实现 ──────────────────► 任一 CLI，**开独占 worktree**
│   └─ 🔴 并行 N 路 ─────────────────────────► N 个 CLI × N 个互不相交 worktree
│
├─ 需用户确认 / 升版 / 提交 / 部署 / 记忆 / 回复 ──► Hermes（绝不派发）
│
└─ 派发门槛：预计 > 4 个工具调用 且 产出是结果而非对话；低于此 Hermes 自己做
```

**写权判据（唯一一条）**：任务要改文件 → 先 `worktree_create`，子代理在**独占 worktree**
内执行；任务只读 → 可直接在主树跑。**不判断「这个 CLI 是不是执行者」**。

**派发前必写**（prompt 契约，工具层不强制）：
- 只读任务：「只读任务，严禁修改、创建、删除任何文件」+「禁止 git add/commit/checkout/reset/stash」
- 执行任务：「只在指定 worktree 内改动，禁止任何 git 写操作」

## 三、工作流（顺序不可跳）

```
1. Hermes            : worktree_create(repo, branch)        （隔离边界）
2. Hermes → 子代理 A : 在 worktree 内实现（可写）          （任一 CLI）
3. Hermes ← 子代理 A : 自述完成（仅线索，非证据）
4. Hermes → 子代理 B : 独立复核 A 的 diff（只读）          （B ≠ A）
5. Hermes            : 在 worktree 内跑真实测试 / 负控
6. Hermes            : 合并回主树 → 升版 → 提交（唯一写入）
7. Hermes            : worktree_remove（junction 安全）
```

并行版：任务 A/B/C 各占一个 worktree，三个 CLI 同时执行，复核交叉进行
（A 的代码交给 B 或 C 复核），最后由 Hermes 统一合并。

- **第 4 步的价值在独立**：同一执行者自己复核 = 没复核。
- **第 5 步的价值在活实例**：exit 0 不证明完成；必须有真实产物/测试读数。

## 四、验收：自述不是证据（四关）

| 关 | 判据 | 反例（必须能变红） |
|---|---|---|
| **可运行** | 真实测试/构建在**隔离实例**跑通，有原始输出 | 「代理说它跑过了」 |
| **可发现** | 产物路径/命令存在且被真实调用 | 生成了但没人引用 |
| **可达** | 端到端调用链打通（非仅单元） | 单元绿、集成断 |
| **默认态** | 默认配置下即为期望行为 | 只在特殊参数下正确 |

🔴 **负控先行**：每条验收门禁先造一个「应当失败」的负控，证明它会变红，再信它报绿
（同 `mechanical-gate-verification`）。
🔴 **「我没改任何文件」的自证不可采信**：核对真实路径的 mtime/内容指纹，
不用 `git status` 条数（gitignored 目录是盲区）。

## 五、驱动各 CLI

| CLI | 无头命令 | 写能力 | 关键约束 |
|---|---|---|---|
| **opencode** | `cli_bridge(action="opencode_run")` | ✅ `agent="build"` | 需写文件时**显式传池内模型**；默认 `space-bunny-free` 被限流 |
| **codex** | `cli_bridge(action="codex_exec", sandbox=...)` | ✅ `workspace-write` | 默认 `read-only`（复核用）；执行时显式传 `workspace-write` |
| **qoderclicn** | `cli_bridge(action="qoder_run", directory=...)` | ✅ | `permission_mode` 必须 `bypass_permissions`；`directory` 必填 = 隔离边界 |
| **worktree** | `cli_bridge(action="worktree_create/list/remove")` | 隔离原语 | **junction 安全**，见下 |

- `worktree_create(repo, branch?, path?, base?)`：`branch` 缺省 `wt/<UTC时间戳>`，
  `path` 缺省 `<repo>-wt-<branch>`；建完回读 `rev-parse --show-toplevel` 核对。
- `worktree_remove(repo, path, force?)`：检测到 worktree 内含 junction/符号链接时**拒绝 `force`**；
  非 force 且工作区脏也拒绝；移除前先 `_delete_links_only`（**只删链接、绝不删目标**）。
  `git worktree remove --force` 会跟随 junction/symlink 递归删除其目标。

### 硬约束

1. 🔴 **驱动任何会读 stdin 的 CLI 都必须关 stdin**（`stdin=DEVNULL`）：stdin 是**保持打开的管道**
   时进程阻塞等 EOF、零输出。opencode 与 codex 同病。
2. 🔴 **qoder 执行子代理的 `permission_mode` 必须是 `bypass_permissions`**：`dont_ask`
   不弹审批 ⇒ 任何写入被直接拒绝。安全来自隔离 worktree，不来自 prompt。
3. 🔴 **`codex review` 的自定义 PROMPT 与 `--uncommitted`/`--base` 互斥**：给了 prompt 就不能带 flag。
4. 🔴 **opencode 默认模型 `space-bunny-free` 被限流**：可写任务必须显式传池内模型
   （`opencode/longcat-2.5-preview-free` 等，查 `opencode models`），或依赖驱动的
   `DEFAULT_OPENCODE_MODEL`。**别信 `agent list` 里 `"permission": "*"` 就等于能跑** ——
   权限放行 ≠ 模型可用。
5. ⚠️ **Windows `.CMD`/`.bat` shim 会截断多行 prompt**：cmd.exe 重新解析 argv 只送第一行。
   驱动时须解析成真实调用（`"...\\x.exe" %*` → `[x.exe]`；`node "...\y.mjs" %*` → `[node, y.mjs]`），
   绕开 cmd.exe；`%~dp0` 展开带尾分隔符，替换后补 `os.sep`。
6. ⚠️ **codex 的 `login status` = Not logged in 不代表故障**：走自定义 provider 时属正常。
7. ✅ **优先用 `cli_bridge`**：已内置关 stdin、`.CMD` shim 解析、`stdout=None` 加固，
   以及 junction 安全的 `worktree_create/list/remove`。

## 六、Pitfalls

- 🔴 **让执行子代理直接写主工作树** ⇒ 单写者律崩，版本号碰撞，验证跑的是中间态。
- 🔴 **执行者同时是复核者** ⇒ 独立性归零。
- 🔴 **信子代理的完成自述** ⇒ 必须核对真实产物与 mtime/指纹。
- 🔴 **把写权按 CLI 固定**（「只有 X 能执行，其余只读」）⇒ 其余执行体沦为监督员，
  分担压力的初衷落空。写权一律由 worktree 边界授予。
- 🔴 **两路并发写同一棵树** ⇒ 无冲突提示的静默覆盖。
- 🔴 **不关 stdin 就驱动 CLI** ⇒ 阻塞等 EOF，被误判成「通道故障」。
- 🔴 **插件放错目录**：profile 的插件目录是 `$HERMES_HOME/plugins/`
  （`profiles/<name>/plugins/`），**不是** `hermes-home/plugins/`（那是 default profile 的）。
  放错 = 永不加载、`plugins list` 也看不到。
- 🔴 **删/改名插件前必须扫「死指引」**：全量 grep 该工具名（含 skill / SOUL.md / 各 profile），
  `count` 必须为 0；替换时逐个 `assert count == 1` 再改（防 replace no-op 假通过）。
- 🔴 **删插件顺序不能反**：① `rm -f <link>`（只解链）并验证目标仍在；
  ② `hermes config set plugins.enabled [...]` 摘掉启用项；
  ③ 确认引用数=0 且 enabled 数=0 后才 `rm -rf`。反序会让 Hermes 启动报错。
- 🔴 **移植外部插件代码时整文件搬运，不要重写**：保住了 stdin/shim/降级链等隐性约束。
  移植后立即 `cmp` 证明逐字节一致，再跑真实任务（不只单测）验证。
- 🔴 **schema 有 `additionalProperties: False` 时，新增 action 的新参数必须同步进 schema**：
  漏一个该 action 调用即被 schema 拒。
- ⚠️ **模型账本不统一**：codex（本地网关）、qoder、opencode 三套降级来源互不知情 ⇒
  「谁在什么模型上跑/为何降级」不可归因。
- ⚠️ **auto_load / plugin 生命周期只解析一次** ⇒ 改完必须重启 Hermes。

## 七、相关

- `external-agent-delegation` —— Hermes ↔ 单代理的委派边界（只读路径的任务性质判定；本 skill 的底座）。
- `opencode-usage` —— OpenCode 用法手册（agent 路由面、stdin/shim 陷阱、模型限流）；
  **角色边界以本 skill 为准**，不在那份里找。
- `multi-session-collaboration` —— 单写者律、版本号串行化、worktree 隔离原语。
- `mechanical-gate-verification` —— 门禁必须能变红。
- `capability-delivery-verification` —— 交付四关（可运行·可发现·可达·默认态）。
