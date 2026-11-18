# 给 AI 的执行单：把 fork 同步回上游最新版（CCAR-workflow）

> 这份文档是给 **AI 助手**看的，不是给人看的。目标只有一个：让 fork `sunwocd/CCAR-workflow`
> 与上游 `wangwingzero/CCAR-workflow` 的 `main` 完全一致，并消除"每天再分叉"的根因。
> 请严格按本文执行，**不要自由发挥、不要改用 merge/PR、不要动上游仓库**。

---

## 0. 背景事实（核实于 2026-11-19，可直接采信，无需重新调查）

> **数量会随每日定时任务漂移**：fork 的 cron 每天北京时间 15:25 跑一次并提交 state，所以"落后多少个提交"
> 每天都会变。**下面的确切数字只是参照，判定依据是"内容特征"而不是数字**：fork 独有提交必须全部是
> `chore: update regulation state <日期>`，改动文件必须只有那 3 个自动生成文件。二者满足即安全。

| 项 | 值 |
|---|---|
| 上游（source） | `wangwingzero/CCAR-workflow`，默认分支 `main` |
| fork | `sunwocd/CCAR-workflow`，默认分支 `main`（注意：owner 是 `sunwocd`，不是 `sunwcod`） |
| 当前关系 | **已分叉（diverged）**：fork 独有若干条 state 提交（11-19 实测 7 条），落后上游 30 余个提交——两个数字每天都在变，以实测为准 |
| 共同祖先 | `6c6e0e3` |
| fork 独有提交（11-19 实测 7 条） | `82e78fd` 11-12、`258f829` 11-13、`2b06f56` 11-14、`8aa10d9` 11-15、`e421c56` 11-16、`fe7cbc9` 11-17、`8d89a81` 11-18，**全部是 `chore: update regulation state <日期>`** |
| 分支保护 | 需执行者自行确认（见第 1 节）；若有保护规则，`--force` 会被拒绝，此时停止并回报 |
| fork 的 Actions | 仍在运行，每天定时提交 state —— **这就是分叉的根因**（最近一次 run `44349342078` 成功于 2026-11-18T16:37Z，产生第 7 条 state 提交） |

### 为什么不能用"普通 merge / 打开 PR"

本地实测把上游 merge 进 fork，会在这 3 个文件上**直接冲突**：

```
JS/manifest.json      CONFLICT
data/downloads.json   CONFLICT
data/regulations.json CONFLICT
```

原因：fork 自己的 workflow 每天也在写这 3 个文件，两边都在写同一批**机器自动生成**的文件。

### 为什么必须"丢弃"fork 的这些提交

1. 它们**不包含任何人工代码改动**，只有自动生成的 JSON/manifest 变化。
2. fork 跑的是**旧版代码**，它产出的数据是已知错误的。对照上游修复后的数据：

   | 分类 | fork（11-17） | 上游（修复后） |
   |---|---|---|
   | 17 港澳台合作 | 39 | 0 |
   | 18 国际公约 | 7 | 39 |
   | 28 建议提案答复 | 239 | 0 |
   | 29 政府网站年度报表 | 17 | 0 |

   即 fork 保留了 47 条**栏目错标**的脏行，还在继续往 17/28/29 里写。合并进来等于把脏数据"洗白"成正常改动。

**结论：正确的同步方式是把 fork 的 `main` 直接重置为上游 `main`（`gh repo sync --force` 的硬重置语义），不是 merge。**

---

## 0.5 前置条件：执行者必须"是" fork owner

GitHub 官方文档明确：*People with write access for a forked repository can sync the fork to the upstream repository*
（[Syncing a fork](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/syncing-a-fork)）。
上游 owner（`wangwingzero`）对该 fork 只有 `pull` 权限，**做不了**同步 —— 这也是当初网页上没有 Sync 按钮的原因。

所以执行本单的 AI 必须满足其一：

- **A. 用 fork owner 本人的凭据**（`gh auth status` 里 active account = `sunwocd`）；或
- **B. 拿一个 fork owner 授权的 fine-grained PAT**，仅限 `sunwocd/CCAR-workflow` 单仓库，权限只要
  **Contents: Read and write**（若要用 API 停 Actions，再加 **Actions: Read and write** / Administration 视页面而定），
  有效期设 1 天，用完让人类立刻撤销。

> **不要把 PAT 写进任何提交、日志、文件或聊天记录**。用 `export GH_TOKEN=...`（临时 shell 变量）或 `gh auth login` 交互输入。

---

## 1. 执行前必须做的一件事：确认没有要保留的人工作品

### 1.1 列出 fork 独有的提交，**每一条都必须是 state 提交**

```bash
gh api repos/wangwingzero/CCAR-workflow/compare/main...sunwocd:main \
  --jq '.commits[] | "\(.sha[0:8]) \(.commit.message | split("\n")[0])"'
```

**判定规则（不要按条数判断，条数每天在涨）**：输出的每一行都必须是
`chore: update regulation state YYYY-MM-DD` 这一种形式。

参考输出（2026-11-19 实测为 7 条）：

```
82e78fd8 chore: update regulation state 2026-11-12
258f8290 chore: update regulation state 2026-11-13
2b06f56c chore: update regulation state 2026-11-14
8aa10d9d chore: update regulation state 2026-11-15
e421c565 chore: update regulation state 2026-11-16
fe7cbc9f chore: update regulation state 2026-11-17
8d89a812 chore: update regulation state 2026-11-18
```

只要出现**任何一条**不是这个格式（例如 `feat:`、`fix:`、`docs:`、带人工描述）→ **立即停止，回报人类**。

### 1.2 确认这些提交只改了自动生成的文件

```bash
gh api "repos/wangwingzero/CCAR-workflow/compare/sunwocd:main~<N>...sunwocd:main" --jq '.files[].filename'
```

把 `<N>` 换成 1.1 里数出来的提交条数（2026-11-19 是 7）。**期望输出只有这 3 行**：

```
JS/manifest.json
data/downloads.json
data/regulations.json
```

若出现 `src/`、`.github/`、`tests/`、`README.md` 等任何其它文件 → **停止执行，回报给人类，不要强推。**

### 1.3 不依赖 `gh` 的等价校验（纯 git，只读）

```bash
git ls-remote https://github.com/sunwocd/CCAR-workflow.git main      # fork 的 main
git ls-remote https://github.com/wangwingzero/CCAR-workflow.git main # 上游的 main
```

两个 SHA 不同 → 确认需要同步，继续往下执行；相同 → 已经一致，可以跳过第 3 节（重置），
但仍要执行第 2 节（停 Actions），否则下一次 cron 又会分叉。

---

## 2. 操作一：先停掉 fork 的定时任务（顺序不能颠倒）

> 若不停，同步完当天（或次日北京时间 15:25）又会被它自己写分叉。

**方式 A（网页，推荐给对方本人点一次）**
`https://github.com/sunwocd/CCAR-workflow/settings/actions` → 选 **Disable actions**。

**方式 B（用 owner 的 token，命令行）**

```bash
gh api -X PUT repos/sunwocd/CCAR-workflow/actions/permissions -F enabled=false
```

> ⚠️ 重要：`gh api repos/sunwocd/CCAR-workflow/actions/permissions` 这种**读取**接口在非 admin 凭据下
> 会返回 `403 You must have repository read permissions or have the repository Actions policies fine-grained permission`。
> 这是权限限制，**不代表失败**。验证请用下面两种方式之一：
>
> ```bash
> # 方式 1：用 owner 自己的凭据（能跑通 PUT 的凭据，读取一般也能跑通）
> gh api repos/sunwocd/CCAR-workflow/actions/permissions --jq '.enabled'
> # 期望：false
> ```
>
> **方式 2（最可靠，无需任何 API 权限）**：打开 `https://github.com/sunwocd/CCAR-workflow/actions`，
> 若顶部出现 “Workflows aren't being run on this forked repository” 之类的提示，或页面没有任何运行记录，
> 即说明已停用；也可直接看 `Settings → Actions → General` 里是否显示 Disabled。

---

## 3. 操作二：把 fork 的 main 重置为上游 main

> 前提：已用 **fork owner（sunwocd）本人**的凭据登录（`gh auth status` 里 active account 必须是 `sunwocd`）。
> 上游 `wangwingzero/CCAR-workflow` 是公开仓库，读取不需要任何权限。

### 方式 A：一条命令（优先，最不容易出错）

```bash
gh repo sync sunwocd/CCAR-workflow \
  --source wangwingzero/CCAR-workflow \
  --branch main \
  --force
```

`--force` 是必需的：不加会因"分支已分叉"拒绝执行（官方帮助原文：*A fast forward update will be used
except when the `--force` flag is specified, then the two branches will be synced using a hard reset*）。

若报权限错误，说明当前 `gh` 登录账号不是 `sunwocd`；先 `gh auth login` 切到 fork owner 账号，
或改用下面的方式 B（把 token 交给 git）。

### 方式 B：git 等价操作（A 失败时用）

```bash
git clone https://github.com/sunwocd/CCAR-workflow.git cc1k-sync
cd cc1k-sync
git remote add upstream https://github.com/wangwingzero/CCAR-workflow.git
git fetch upstream main
git checkout main
git reset --hard upstream/main
git push --force-with-lease origin main
```

> 用 `--force-with-lease` 而不是 `--force`；若被拒绝说明这期间远端又变了，重新 `git fetch` 后再推。
> 认证：可用 `gh auth setup-git` 让 git 复用 gh 的凭据，或 `git clone https://<TOKEN>@github.com/...`（用完清理 shell 历史）。

### 方式 C：只有浏览器时（不装任何工具）

1. 打开 `https://github.com/sunwocd/CCAR-workflow`
2. 看到黄色横幅 “This branch is N commits ahead of and M commits behind wangwingzero/CCAR-workflow:main”
   （N/M 每天在变，2026-11-19 实测为 7 ahead / 30+ behind）
3. 点右侧 **Sync fork**

⚠️ **但要注意**：GitHub 官方当前文档写的是——*If the changes from the upstream repository cause conflicts,
GitHub will prompt you to create a pull request to resolve the conflicts*（[官方文档](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/syncing-a-fork)）。
本仓库的情况正是"有冲突"（3 个自动生成文件），所以网页**大概率不给 Update branch，而是把你推向 PR**。
**一旦被推向 PR：放弃网页路径，改用方式 A 的 `gh repo sync --force`**，不要真的去建那个 PR（建了就要手解 3 个生成文件的冲突，等于把脏数据带回来）。

---

## 4. 操作三：验证同步结果（缺失任何一条都算失败）

### 4.1 两个仓库的 main 必须完全相同

```bash
gh api repos/wangwingzero/CCAR-workflow/compare/main...sunwocd:main \
  --jq '"\(.status) ahead=\(.ahead_by) behind=\(.behind_by)"'
# 期望：identical ahead=0 behind=0
```

若 `gh` 不可用，用纯 git（只读，无需登录）：

```bash
a=$(git ls-remote https://github.com/sunwocd/CCAR-workflow.git main | cut -f1)
b=$(git ls-remote https://github.com/wangwingzero/CCAR-workflow.git main | cut -f1)
echo "fork=$a"; echo "upstream=$b"
[ "$a" = "$b" ] && echo "✅ identical" || echo "❌ still diverged"
```

### 4.2 脏数据必须已被清除（关键业务校验）

> 下面的命令示例按 bash / Linux / macOS 写。Windows PowerShell 里把 `/tmp/reg.json` 换成 `$env:TEMP\reg.json`、
> `python3` 换成 `python`，其余不变。

```bash
curl -sL "https://raw.githubusercontent.com/sunwocd/CCAR-workflow/main/data/regulations.json" -o /tmp/reg.json
python3 - <<'PY'
import json
docs = json.load(open("/tmp/reg.json"))["documents"]
counts = {k: len(v) for k, v in docs.items() if v}
print("total:", sum(counts.values()))
print(counts)
assert counts.get("17", 0) == 0, "❌ 17 港澳台合作 仍有脏行"
assert counts.get("28", 0) == 0, "❌ 28 建议提案答复 仍有脏行"
assert counts.get("29", 0) == 0, "❌ 29 政府网站年度报表 仍有脏行"
assert counts.get("18", 0) > 0, "❌ 18 国际公约 应有数据"
print("✅ 脏数据已清除")
PY
```

**判定标准**（上游当前基线，数量会随每日 CI 小幅增长，不必逐位相等）：

- 17 / 28 / 29 三类 **必须为 0 或不存在**（这是本次同步的核心目的）
- 18 国际公约 **必须大于 0**（约 39 条）
- total 约 1690～1700
- 若 17/28/29 仍是 39 / 239 / 17（fork 11-17 的旧值），说明**同步没生效**，或是不小心做了 merge 而非 reset —— 回到第 3 节重做

### 4.3 代码版本校验

```bash
curl -sL "https://raw.githubusercontent.com/sunwocd/CCAR-workflow/main/src/search_api.py" | grep -c "GATSHHZ"
# 期望：>= 1（这是 v2 修复才有的栏目映射；旧代码里没有）
```

---

## 5. 操作四：确认以后不会再分叉（上游已加防护）

### 上游已经在代码层面修掉了根因

上游 `wangwingzero/CCAR-workflow` 的 `.github/workflows/check-updates.yml` 里，`Commit state update`
这一步已经加了仓库名守卫：

```yaml
      - name: Commit state update
        if: github.repository == 'wangwingzero/CCAR-workflow'
        run: |
          ...
```

含义：**只有上游自己会提交 state；任何 fork 跑同一个 workflow 都会跳过这一步**（`github.repository`
在 fork 里是 `sunwocd/CCAR-workflow`，条件为假）。所以只要 fork 同步到包含该守卫的版本，就**自动变成只读跟随**，
不需要手工注释任何步骤。

### 因此操作四只剩"确认"这一件事

同步完成后（此时 Actions 仍处于第 2 节所设的 disabled 状态），按顺序做：

1. **确认 fork 拿到了守卫**：

```bash
curl -sL "https://raw.githubusercontent.com/sunwocd/CCAR-workflow/main/.github/workflows/check-updates.yml" \
  | grep -n "github.repository == 'wangwingzero/CCAR-workflow'"
# 期望：输出 1 行（带行号）
```

2. **重新启用 Actions**（此时已安全）：

```bash
gh api -X PUT repos/sunwocd/CCAR-workflow/actions/permissions -F enabled=true
```

   或在网页 `Settings → Actions → General` 选 **Allow all actions**。

3. **手动跑一次验证**（可选但推荐）：

```bash
gh workflow run check-updates.yml --repo sunwocd/CCAR-workflow -f days=1 -f notify=0
```

   跑完后检查该 run 的日志里 **`Commit state update` 这一步应当是 skipped**，
   并且 `https://github.com/wangwingzero/CCAR-workflow/compare/main...sunwocd:main` 仍然显示
   `identical ahead=0 behind=0`。

### 若上面第 1 条确认失败（fork 版本里没有守卫）

说明同步没拿到最新上游。**不要手工注释步骤**，回到第 3 节重新同步。手工改 fork 文件本身会造成
新的分叉（fork 会多一个"自己改 workflow"的提交）。

### 补充：静态发布与 R2 无需处理

`Publish data JSON to static server` 步骤自带守卫：`STATIC_SSH_KEY / STATIC_SSH_HOST / STATIC_SSH_USER /
STATIC_ROOT` 四个 secret 缺任一就打印 `ℹ️ Skipping static server upload (SSH secrets not fully configured)`
并 `exit 0`。fork 近期日志也显示 `R2 upload disabled, missing env vars`。因此 fork 不会覆盖线上数据，不必改动。

### 禁止项

- **不要**手工注释 fork 里的 `Commit state update` 步骤——上游守卫已覆盖，手工改只会引入新分叉。
- **不要**给 fork 配置 `repo-sync/github-sync` 之类的自动同步 workflow：它与本仓库的每日 state 提交会互相打架。

### ⚠️ 唯一需要留意的副作用

该守卫是**字符串硬编码**了 `wangwingzero/CCAR-workflow`。如果上游 owner 将来**重命名或转移仓库**，
`github.repository` 会变，条件失效 → **上游自己的 state 也不再提交**（CI 会绿，但数据不再更新，属于静默失败）。
届时只需把这一行改成新的 `owner/repo`。执行本单的 AI 不需要处理它，知道即可：

```bash
# 排查"CI 一直绿但 data 不再变化"时，先看这一步是否 skipped
gh run view <run-id> --repo wangwingzero/CCAR-workflow --log | grep -i "Commit state update"
```

---

## 6. 严禁事项（给 AI 的硬约束）

1. **绝对不要**对 `wangwingzero/CCAR-workflow`（上游）做任何写操作：不 push、不改 settings、不改 secrets、不建 PR 指向它的分支、不删任何东西。
2. **不要**删除或重建 fork（会丢 star/watch/fork 关系），用重置即可。
3. **不要**只 force push `main` 以外的分支；**不要**顺手清理 fork 上的其他分支。
4. **不要**用 merge / cherry-pick / revert 的方式"解决"冲突——那正是本文明确禁止的路径。
5. **不要**在 fork 里提交任何新文件或改任何文件（包括 workflow）——那本身就会制造新分叉。
6. 若第 1 节的校验输出与"期望"不符，或遇到任何 require review / branch protection 报错：**停止并回报**，不要绕过。
7. 凭据只用 owner 本人授权的、仅限 `sunwocd/CCAR-workflow` 单仓库、`Contents: Read and write` 的最小权限 token；用完请提醒人类撤销。

---

## 7. 一句话总结给人类

> fork 会分叉，是因为它自己也在每天提交自动生成的 state 文件，而上游同样在写同一批文件，双方数据必然冲突。
> 上游现在已加守卫（只有 `wangwingzero/CCAR-workflow` 才提交 state），所以给 fork 的正确做法是：
> **停 Actions → `gh repo sync --force` 把 main 硬重置为上游 → 验证 17/28/29 脏数据清零 → 重新启用 Actions**。
