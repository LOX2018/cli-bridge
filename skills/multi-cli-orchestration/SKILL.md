---
name: multi-cli-orchestration
description: "Use when 把本机多个编码 CLI（opencode/qoderclicn/codex 等）纳入 Hermes 统一编排：角色分配·自动分派·隔离写权·独立验收。"
version: 1.3.0
author: LOX (通过对话提炼)
license: Proprietary
platforms: [windows]
metadata:
  hermes:
    tags: [orchestration, delegation, multi-cli, opencode, qoderclicn, codex, role-assignment, worktree-isolation, verification, sole-writer]
    related_skills: [external-agent-delegation, opencode-driven-development, multi-session-collaboration, subagent-delegation, mechanical-gate-verification, capability-delivery-verification]
---

# 多 CLI 统一编排（Hermes 为唯一入口）

> 适用：本机装有多个编码 CLI，希望 **Hermes 作为唯一入口**，按任务性质
> **自动分派**给对应 CLI，监督其工作并**独立验收**其成果。
>
> 与 `external-agent-delegation` 的分工：那份管 **Hermes ↔ 单个外部代理** 的
> 委派边界；本份管 **多 CLI 的角色编排**（谁写、谁审、谁只读、谁独占合并）。

---

## 一、角色契约（唯一权威表）

> 🔴 **2026-10-06 架构调整**：原表把写权**按 CLI 固定**（qoderclicn 唯一执行者、
> 其余只读），后果是三个 CLI 只有一个在干活，其余沦为监督员。
> 现改为**按任务分派**：三个 CLI **都是执行者**，写权由 **worktree 边界**授予，
> **不由「该代理是否被指定为只读」授予**。

| 角色 | 承担者 | 权限边界 | 判据来源 |
|---|---|---|---|
| **唯一入口 / 仲裁者 / 唯一提交者** | **Hermes** | 写主工作树·升版·提交·部署·记忆·回复 | 署名可归属；审计轨迹唯一锚点 |
| **编码子代理（可并行，任一 CLI 皆可）** | **opencode / codex / qoderclicn** | **独占 worktree 内可写**；主树对子代理只读 | 三个 CLI 实测均可写文件（见 §五） |
| **复核者（须与执行者分离）** | 任一**未参与该任务实现**的 CLI | **只读**复核 diff | 写码者不能自评；独立性来自「不是同一代理」 |
| **并行只读取证**（可选） | Hermes `delegate_task` | **只读** | 独立对账；**禁止冒充外部代理** |

### 三条不可协商的分离律

1. **写权来自 worktree 边界，不来自代理身份**：三个 CLI 都可执行。安全靠**独占
   worktree**，不靠「只给一个代理写权」。
2. **执行者 ≠ 复核者**：谁实现，谁不得复核自己的实现。复核必须由**另一个** CLI 承担。
   （原「审计者 ≠ 执行者」是这条在固定角色下的特例。）
3. **子代理永不允许 git 写操作**：`add` / `commit` / `push` / `checkout` / `reset` / `stash`
   一律禁止；所有提交由 Hermes 独占。

### 并发写权隔离（并行执行的唯一安全前提）

N 路并行执行 = N 个**互不相交**的 worktree。**同一棵源码树不得有两个写者**
（last-writer-wins 静默丢改动，无冲突提示，见 `multi-session-collaboration`）。
隔离原语必须 **junction 安全**：删 worktree 前先解链，**绝不 `--force` 递归含链接的树**。

---

## 二、自动分派决策树（用户不需手动强调）

```
任务到达
├─ 产出是「结果」（报告/方案/diff/评审/上游情报）？ ──► 只读路径
│   ├─ 代码审计/架构/探索/调研/知识库/依赖分析 ──► 任一 CLI 只读跑
│   ├─ 需要「改动的评审」或第二意见 ──────────► 另一个 CLI 只读复核
│   └─ 独立交叉对账 ──────────────────────────► Hermes delegate_task（只读）
│
├─ 产出是「可运行的代码改动」？ ──► 执行路径（三选一，按任务分派）
│   ├─ 单点精准修改（Hermes 已知确切改法）────► Hermes 自己改（不派发）
│   ├─ 多文件/需探索的实现 ──────────────────► 任一 CLI，**开独占 worktree**
│   └─ 🔴 并行 N 路 ─────────────────────────► N 个 CLI × N 个互不相交 worktree
│
├─ 需用户实时确认 / 版本递增 / 提交 / 部署 / 记忆 / 回复 ──► Hermes（绝不派发）
│
└─ 派发门槛：预计 > 4 个工具调用 且 产出是结果而非对话；低于此 Hermes 自己做
```

**写权判据（唯一一条）**：任务要改文件 → 先 `worktree_create`，子代理在**独占 worktree**
内执行；任务只读 → 可直接在主树跑。**不判断「这个 CLI 是不是执行者」**。

**派发前必写**（prompt 契约，工具层不强制）：只读任务写
「只读任务，严禁修改、创建、删除任何文件」+「禁止 git add/commit/checkout/reset/stash」；
执行任务写「只在指定 worktree 内改动，禁止任何 git 写操作」。

---

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

**并行版（真正分担压力）**：任务 A/B/C 各占一个 worktree，三个 CLI 同时执行；
复核交叉进行（A 的代码交给 B 或 C 复核），最后由 Hermes 统一合并。

**第 4 步的价值在独立**：同一执行者自己复核 = 没复核。
**第 5 步的价值在活实例**：exit 0 不证明完成；必须有真实产物/测试读数。

---

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

---

## 五、驱动各 CLI 的实测要点（2026-10-06 活实例取证）

| CLI | 无头命令 | 写能力（实测） | 关键约束 |
|---|---|---|---|
| **opencode**（执行/审计） | `cli_bridge(action="opencode_run")`，agent=`build` | ✅ 可写（build agent 权限 `"* allow"`） | **模型默认必须避开 `space-bunny-free`**（共享免费池，实测 `Rate limit exceeded`）；用池内模型，如 `opencode/longcat-2.5-preview-free` |
| **codex**（执行/复核） | `cli_bridge(action="codex_exec", sandbox=...)` | ✅ `workspace-write` 可写 | **默认 `read-only`**（复核用）；执行时显式传 `workspace-write`。`codex review` 无沙箱参数，天然只读。**驱动须关 stdin** |
| **qoderclicn**（执行） | `cli_bridge(action="qoder_run", directory=<隔离worktree>)` | ✅ 可写 | `permission_mode` 必须 `bypass_permissions`（`dont_ask` 拒写）。`directory` 必填 = 隔离边界 |
| **worktree** | `cli_bridge(action="worktree_create/list/remove")` | 隔离原语 | **junction 安全**，见下 |

> 三个 CLI 的写能力均为 2026-10-06 实测：真实创建文件、读回内容、比对产物目录。
> 结论是**能力从来不是瓶颈**，写权分配才是 —— 所以角色按任务分派，不按 CLI 固定。

🔴 **codex 驱动必须关 stdin**（2026-10-05 实测根因）：codex 检测到 stdin 是**未关闭的管道**时，
打印 `Reading additional input from stdin...` 并**阻塞等待 stdin EOF** ⇒ 表现为"挂死"。
**判据：驱动 codex / 任何会读 stdin 的 CLI，必须 `stdin=DEVNULL` 或关闭管道**，
并把长任务放进**后台/长时**通道，不要用一次性短超时下结论。

**其他实测事实**：codex 运行时自报 v0.160.0（启动器 `--version`=0.157.1，版本漂移）；
`Model metadata for 'auto:lox' not found → fallback` 告警；`codex login status`=Not logged in
**不代表故障**（走自定义 provider `freellmapi`→`127.0.0.1:31415`，非官方 OAuth）。

### opencode 模型选择（2026-10-06 实测）

`opencode models` 可查池内真实模型。**实测能写**：`opencode/longcat-2.5-preview-free`、
`opencode/nemotron-3.5-lightning-free`；`opencode/space-bunny-free`（**内置默认**）被限流。

🔴 **判据：调用 `opencode_run` 时若任务需要写文件，必须显式传池内模型**，
或依赖驱动的 `DEFAULT_OPENCODE_MODEL`（已设为 `longcat-2.5-preview-free`）。
**别信 `agent list` 里 build 的 `"permission": "*"` 就等于能跑** —— 权限是放行的，
但模型被限流一样跑不动。

### 五条全链路实测发现的硬约束

1. 🔴 **驱动任何会读 stdin 的 CLI 都必须关 stdin**：codex 与 **opencode** 同病——stdin 是**保持打开的管道**（Electron/agent 进程正是如此）时会**挂死等 EOF、零输出**。已给 opencode 插件 7 处 `subprocess` 调用补 `stdin=DEVNULL`（修复前 Hop1 空输出、修复后正常）。
2. 🔴 **qoder 执行子代理的 `permission_mode` 必须是 `bypass_permissions`**：`dont_ask` 不弹审批 ⇒ **任何写入被直接拒绝**（实测 Hop2 落盘失败）。安全来自**隔离 worktree**，不来自 prompt。
3. 🔴 **`codex review` 的自定义 PROMPT 与 `--uncommitted`/`--base` 互斥**（实测报错 `the argument '--uncommitted' cannot be used with '[PROMPT]'`）：给 prompt 就不能带 flag，反之亦然。`cli_bridge` 已按此实现。
4. 🔴 **opencode 默认模型 `space-bunny-free` 被限流**（2026-10-06 实测）：共享免费池报 `Rate limit exceeded`。可写任务必须显式传池内模型如 `opencode/longcat-2.5-preview-free`（或驱动内置的 `DEFAULT_OPENCODE_MODEL`）。**别信 `agent list` 里 build 的 `"permission": "*"` 就等于能跑** —— 权限放行 ≠ 模型可用。查池内真实模型：`opencode models`。
5. ✅ **worktree 生命周期原语已内置（cli-bridge v1.2.0）**：`worktree_create` / `worktree_list` / `worktree_remove` 三个 action 自动化「给执行子代理开隔离 worktree / 审计 / 回收」，调用方不必手管写权边界。
   - `worktree_create(repo, branch?, path?, base?)`：`git worktree add -b`；`branch` 缺省 `wt/<UTC时间戳>`，`path` 缺省 `<repo>-wt-<branch>`；建完回读 `rev-parse --show-toplevel` 核对。
   - `worktree_remove(repo, path, force?)`：**junction 安全**——检测到 worktree 内含 junction/符号链接时**拒绝 `force`**；非 force 且工作区脏也拒绝；移除前先 `_delete_links_only`（**只删链接、绝不删目标**）再 `git worktree remove`（不加 `--force`）。
   - 🔴 **为什么必须这样**：`git worktree remove --force` 会**跟随 junction/符号链接递归删除其目标**——实测清空过主仓 `node_modules`（事故，见 `multi-session-collaboration` §八）。**判据：绝不让 `--force` 递归一个含链接的树；先删链接本身（`rmdir` 对 junction 只解链、不递归），再 remove。**
   - 实机验证 12/12（真实 git worktree + 真实 `mklink /J`）：create ok；list ok；非 git/已存在路径拒绝；force+junction **被拒**（links=1）；脏工作区拒绝；`_delete_links_only` 删链后**目标存活**；干净 remove ok；缺失路径拒绝；handler 分发 ok；未知 action 拒绝。

---

## 六、Pitfalls

- 🔴 **让执行子代理直接写主工作树** ⇒ 单写者律崩，版本号碰撞，验证跑的是中间态。
- 🔴 **执行者同时是复核者** ⇒ 独立性归零。
- 🔴 **信子代理的完成自述** ⇒ 已有实测反例（2/3 子代理输出退化、CSS 变量误报 14→实 7）。
- 🔴 **把写权按 CLI 固定**（如「只有 qoder 能执行，其余只读」）⇒ 三个 CLI 只有一个在干活，其余沦为监督员，分担压力的初衷落空。三个 CLI 的写能力均已实测（opencode build agent 权限全放行、codex `workspace-write`、qoder `bypass_permissions`）。**能力从来不是瓶颈，写权分配才是** —— 写权一律由 worktree 边界授予。
- 🔴 **两路并发写同一棵树** ⇒ 无冲突提示的静默覆盖。
- 🔴 **不关 stdin 就驱动 codex** ⇒ 它阻塞等 EOF，被误判成"通道故障"（实测踩到）。
- 🔴 **把 opencode 钉成只读「项目经理」**（2026-10-06 纠正）：旧 `opencode-driven-development` SKILL.md 首行即写 HARD RULE —— OpenCode does not modify files. Ever.，但实测 opencode 的 build agent 权限全放行、可真实落盘。**判据：角色文档与实测权限不一致时以实测为准** —— 该 skill 已改名 `opencode-usage`（收敛为用法手册），角色契约上收至本 skill。
- 🔴 **插件放错目录**：profile 的插件目录是 **`$HERMES_HOME/plugins/`**（此处 `$HERMES_HOME` = **profile 根**，即 `profiles/lox/plugins/`），**不是** `hermes-home/plugins/`（那是 default profile 的）。放错=永不加载、`plugins list` 也看不到（实测踩到）。
- ⚠️ **本地自建插件要用 `hermes plugins list` 确认 `enabled`**：CLI 的 `enable` 依赖 provenance 台账，自建目录不在册会报 "No plugin named"；直接把它写进 config 的 `plugins.enabled` 即可（运行时 discovery 扫 `$HERMES_HOME/plugins/*/plugin.yaml`）。
- ⚠️ **模型账本不统一**：codex（本地网关 31415）、qoder（qoder2api）、opencode 插件
  （硬编码降级链）三套来源互不知情 ⇒ 「谁在什么模型上跑/为何降级」不可归因。
- ⚠️ **auto_load / plugin 生命周期只解析一次** ⇒ 改完必须重启 Hermes，当前会话不自动生效。
- 🔴 **删除/改名插件前必须扫「死指引」，否则工具消失但文档还在教人调它**（2026-10-06 实测）：
删除独立 `opencode` 插件并把能力迁入 `cli-bridge` 后，`opencode-driven-development` SKILL.md 里
**8 处 `opencode(...)` 调用示例**全部变成指向已删工具的死指引 —— 单测全绿、插件也删干净了，
但下次按文档派发必然失败。**判据：删/改名任何工具后，全量 grep 该工具名（含 skill / SOUL.md /
各 profile），`count` 必须为 0**；替换时逐个 `assert count == 1` 再改（防 replace no-op 假通过）。
- 🔴 **删插件的顺序不能反：先解链 → 再改 config → 最后删目录**（2026-10-06 实测）：
profile 里的 `plugins/opencode` 是 **symlink**（不是 junction），但按 junction 纪律处理更安全 ——
① `rm -f <link>`（只解链）并验证**目标仍在**；② 用 `hermes config set plugins.enabled [...]`
摘掉启用项（**当前活跃 profile 的 config.yaml 被工具拒绝直改，必须走 CLI 通道**）；
③ 确认「引用链接数=0 且 enabled 数=0」后，才 `rm -rf` 目录。反序会让 Hermes 启动报错。
- 🔴 **移植外部插件代码时整文件搬运，不要重写**（2026-10-06 实测）：把上游 `opencode_tool.py`
**逐字节复制**为 `opencode_driver.py` 再包一层薄 wrapper，保住了 7 处 `stdin=DEVNULL`、
`.CMD` shim 解析、事件流解析、限流降级链等实测换来的行为；自造版本极易漏掉这些隐性约束。
**判据：移植后立即 `cmp` 证明逐字节一致，再跑真实任务（不只单测）验证行为未变。**
- 🔴 **schema 有 `additionalProperties: False` 时，新增 action 的新参数必须同步进 schema**：
本次加了 `agent` / `variant` / `session_id` / `files`，漏一个则该 action 调用即被 schema 拒。
**判据：新增 action 后，逐个确认其用到的参数都在 `properties` 里。**
- ✅ **`cli_bridge` 工具已封装 opencode / qoderclicn / codex**（插件 `cli-bridge`，**全局**）：装在 `$HERMES_HOME/plugins/`，junction 链接进每个 profile，`default`/`lox`/`wx-llm` 均已 `enabled`）：优先用它而非裸命令——它已内置「关 stdin」「`.CMD` shim 解析（防多行 prompt 截断）」「`stdout=None` 加固」，并内置 junction 安全的 `worktree_create` / `worktree_list` / `worktree_remove`。`qoder_run` 的 `directory` 必填，是隔离边界。

---

## 七、相关

- `external-agent-delegation` —— Hermes ↔ 单代理的委派边界（只读路径的任务性质判定；本 skill 的底座）。
- `opencode-usage` —— OpenCode 的用法手册（agent 路由面、stdin/shim 陷阱、模型限流）；**角色边界以本 skill 为准**，不在那份里找。
- `multi-session-collaboration` —— 单写者律、版本号串行化、worktree 隔离原语。
- `mechanical-gate-verification` —— 门禁必须能变红。
- `capability-delivery-verification` —— 交付四关（可运行·可发现·可达·默认态）。
