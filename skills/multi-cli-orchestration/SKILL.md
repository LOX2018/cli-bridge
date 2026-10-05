---
name: multi-cli-orchestration
description: "Use when 把本机多个编码 CLI（opencode/qoderclicn/codex 等）纳入 Hermes 统一编排：角色分配·自动分派·隔离写权·独立验收。"
version: 1.1.0
author: LOX (通过对话提炼)
license: Apache-2.0
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
> 只读委派；本份管 **多 CLI 的角色编排**（谁写、谁审、谁只读、谁独占合并）。

---

## 一、角色契约（唯一权威表）

| 角色 | 承担者 | 权限边界 | 判据来源 |
|---|---|---|---|
| **唯一入口 / 仲裁者 / 唯一提交者** | **Hermes** | 写主工作树·升版·提交·部署·记忆·回复 | 署名可归属；审计轨迹唯一锚点 |
| **只读项目经理** | **opencode** | **只读**：审计·架构·调研·探索·否定方向 | 与既有 `opencode-driven-development` 一致，**不变** |
| **执行子代理（实现）** | **qoderclicn** | **隔离 worktree 内可写**；主树禁写 | 已实测可无头驱动（`-p`，exit 0）；原生 `--worktree` |
| **独立评审官** | **codex** | **只读**（`codex review --uncommitted`） | 已实测可用（`codex exec` exit 0）；**驱动须关 stdin** |
| **并行只读取证**（可选） | Hermes `delegate_task` | **只读** | 独立对账；**禁止冒充外部代理** |

### 三条不可协商的分离律

1. **审计者 ≠ 执行者**：opencode 只审计，qoderclicn 只实现。一旦同一 CLI 既审计又写码，
   「写码者不能自己复核」的独立性即失效。
2. **执行者 ≠ 提交者**：执行子代理**永不允许** `git add/commit/push/checkout/reset/stash`；
   所有提交由 Hermes 独占。
3. **并发前提是目录隔离**：N 路执行必须 N 个 worktree，目录不相交；
   **同一棵源码树不得有两个写者**（last-writer-wins 静默丢改动，见 `multi-session-collaboration`）。

---

## 二、自动分派决策树（用户不需手动强调）

```
任务到达
├─ 产出是「结果」（报告/方案/diff/评审/上游情报）？ ──► 只读路径
│   ├─ 代码审计/架构/探索/调研/知识库/依赖分析 ──► opencode（只读）
│   ├─ 需要「改动的评审」或第二意见 ──────────► codex review（只读）
│   └─ 独立交叉对账 ──────────────────────────► Hermes delegate_task（只读）
│
├─ 产出是「可运行的代码改动」？
│   ├─ 单点精准修改（Hermes 已知确切改法）────► Hermes 自己改（不派发）
│   └─ 多文件/需探索的实现 ──────────────────► qoderclicn（隔离 worktree 内可写）
│
├─ 需用户实时确认 / 版本递增 / 提交 / 部署 / 记忆 / 回复 ──► Hermes（绝不派发）
│
└─ 派发门槛：预计 > 4 个工具调用 且 产出是结果而非对话；低于此 Hermes 自己做
```

**派发前必写**（prompt 契约，工具层不强制）：只读任务写
「只读任务，严禁修改、创建、删除任何文件」+「禁止 git add/commit/checkout/reset/stash」；
执行任务写「只在指定 worktree 内改动，禁止任何 git 写操作」。

---

## 三、工作流（顺序不可跳）

```
1. Hermes → opencode : 审计 X，给方案，不要改            （只读）
2. Hermes ← opencode : findings + 建议方案
3. Hermes → qoderclicn : 在 <隔离 worktree> 内实现方案    （可写，限 worktree）
4. Hermes ← qoderclicn : 自述完成（仅线索，非证据）
5. Hermes → codex    : 独立复核该 worktree 的改动          （只读）
6. Hermes            : 在隔离目录跑真实测试 / 负控
7. Hermes            : 合并回主树 → 升版 → 提交（唯一写入）
```

**第 5 步的价值在独立**：同一执行者自己复核 = 没复核。
**第 6 步的价值在活实例**：exit 0 不证明完成；必须有真实产物/测试读数。

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

## 五、驱动各 CLI 的实测要点（2026-10-05 活实例取证）

| CLI | 无头命令 | 关键约束 |
|---|---|---|
| **opencode** | Hermes `opencode` 工具（已装插件） | 复用插件内的 headless 加固 |
| **qoderclicn** | `cli_bridge(action="qoder_run", directory=<隔离worktree>)` | 底层 `qoderclicn -p --permission-mode dont_ask -w <dir>`；**directory 必填=隔离边界** |
| **codex** | `cli_bridge(action="codex_review", directory=<repo>)` | 底层 `codex review --uncommitted`（只读）；另有 `codex_exec` 只读沙箱 |

🔴 **codex 驱动必须关 stdin**（2026-10-05 实测根因）：codex 检测到 stdin 是**未关闭的管道**时，
打印 `Reading additional input from stdin...` 并**阻塞等待 stdin EOF** ⇒ 表现为"挂死"。
**判据：驱动 codex / 任何会读 stdin 的 CLI，必须 `stdin=DEVNULL` 或关闭管道**，
并把长任务放进**后台/长时**通道，不要用一次性短超时下结论。

**其他实测事实**：codex 运行时自报 v0.160.0（启动器 `--version`=0.157.1，版本漂移）；
`Model metadata for 'auto:lox' not found → fallback` 告警；`codex login status`=Not logged in
**不代表故障**（走自定义 provider `freellmapi`→`127.0.0.1:31415`，非官方 OAuth）。

### 三条首期全链路（2026-10-05）实测发现的硬约束

1. 🔴 **驱动任何会读 stdin 的 CLI 都必须关 stdin**：codex 与 **opencode** 同病——stdin 是**保持打开的管道**（Electron/agent 进程正是如此）时会**挂死等 EOF、零输出**。已给 opencode 插件 7 处 `subprocess` 调用补 `stdin=DEVNULL`（修复前 Hop1 空输出、修复后正常）。
2. 🔴 **qoder 执行子代理的 `permission_mode` 必须是 `bypass_permissions`**：`dont_ask` 不弹审批 ⇒ **任何写入被直接拒绝**（实测 Hop2 落盘失败）。安全来自**隔离 worktree**，不来自 prompt。
3. 🔴 **`codex review` 的自定义 PROMPT 与 `--uncommitted`/`--base` 互斥**（实测报错 `the argument '--uncommitted' cannot be used with '[PROMPT]'`）：给 prompt 就不能带 flag，反之亦然。`cli_bridge` 已按此实现。
4. 🔴 **Windows npm/launcher `.CMD` shim 会截断多行 prompt（一类缺陷，opencode 与 codex 都中招）**：`.CMD` 经 cmd.exe 重新解析 argv，**只把第一行送到 CLI**，后续行丢失（实测：多行 prompt 的第二行唯一 token 未到达；改用真实 `.exe` / `node launcher.mjs` 后到达）。**判据：驱动 Windows CLI 时，把 `.cmd`/`.bat` 解析成真实调用**（`"...\x.exe" %*` → `[exe]`；`node "...\y.mjs" %*` → `[node, y.mjs]`），绕开 cmd.exe。注意 `%~dp0` 展开**带尾分隔符**，替换时要补 `os.sep`（否则 `binlauncher.mjs` 少一个反斜杠）。opencode 插件 v1.0.2、cli-bridge v1.0.1 已修。
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
- 🔴 **两路并发写同一棵树** ⇒ 无冲突提示的静默覆盖。
- 🔴 **不关 stdin 就驱动 codex** ⇒ 它阻塞等 EOF，被误判成"通道故障"（实测踩到）。
- 🔴 **插件放错目录**：profile 的插件目录是 **`$HERMES_HOME/plugins/`**（此处 `$HERMES_HOME` = **profile 根**，即 `profiles/lox/plugins/`），**不是** `hermes-home/plugins/`（那是 default profile 的）。放错=永不加载、`plugins list` 也看不到（实测踩到）。
- ⚠️ **本地自建插件要用 `hermes plugins list` 确认 `enabled`**：CLI 的 `enable` 依赖 provenance 台账，自建目录不在册会报 "No plugin named"；直接把它写进 config 的 `plugins.enabled` 即可（运行时 discovery 扫 `$HERMES_HOME/plugins/*/plugin.yaml`）。
- ⚠️ **模型账本不统一**：codex（本地网关 31415）、qoder（qoder2api）、opencode 插件
  （硬编码降级链）三套来源互不知情 ⇒ 「谁在什么模型上跑/为何降级」不可归因。
- ⚠️ **auto_load / plugin 生命周期只解析一次** ⇒ 改完必须重启 Hermes，当前会话不自动生效。
- ✅ **`cli_bridge` 工具已封装 qoderclicn / codex**（插件 `cli-bridge`，**全局**：装在 `$HERMES_HOME/plugins/`，junction 链接进每个 profile，`default`/`lox`/`wx-llm` 均已 `enabled`）：优先用它而非裸命令——它已内置「关 stdin」「`.CMD` shim 解析（防多行 prompt 截断）」「`stdout=None` 加固」，并内置 junction 安全的 `worktree_create` / `worktree_list` / `worktree_remove`。`qoder_run` 的 `directory` 必填，是隔离边界。

---

## 七、相关

- `external-agent-delegation` —— Hermes ↔ 单代理的只读委派边界（本 skill 的底座）。
- `opencode-driven-development` —— opencode 的只读项目经理定位。
- `multi-session-collaboration` —— 单写者律、版本号串行化、worktree 隔离原语。
- `mechanical-gate-verification` —— 门禁必须能变红。
- `capability-delivery-verification` —— 交付四关（可运行·可发现·可达·默认态）。
