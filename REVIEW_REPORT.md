# CCAR Workflow 全面审查与修复报告

审查对象：`D:\CCAR-workflow`（HEAD `451b7d2` + 本次工作树改动）
审查范围：架构与流程、数据一致性、下载/重试可靠性、WAF 与附件解析、状态与 JS 导出、通知、R2、CI、脚本、文档、测试
审查方式：逐文件通读 + 现有 44 个单元测试基线 + 新增针对性测试 + 静态编译/冒烟验证
结论：两轮共修复 44 项缺陷（含 4 项会造成数据丢失/永久缺文件/通知洪水的高风险问题），测试从 44 个增加到 **96 个且全部通过**；数据文件与 JS 导出不再产生 CRLF 污染，历史错标分类已离线修复，4 个 GitHub Actions 已 pin 到 commit SHA；另追加心跳通知的“其他分类新增 N 条”摘要（见第十一节）。

---

## 一、高风险问题（已修复）

### 1. 新增监控分类时会把该分类全部历史文档当“新增”推送
- **位置**：`src/main.py` 增量分支
- **根因**：`known_total` 只按“本次抓到的全部分类”求和判断基线；只要 state 里已有任意文档，新增分类中的每一条都会进入 `new_documents`，触发下载与通知洪水。
- **修复**：逐分类计算 `baseline_categories`（state 中不存在的分类），把这些分类从 `new/updated/reposted` 中剔除，只记录基线并打印 warning；已有分类的增量逻辑不变。
- **测试**：`tests/test_main.py::BaselineCategoryFlowTests`（state 仅 13，爬取 13+14 → 只通知/下载 13，14 记基线）。

### 2. 下载失败后永久不再重试
- **位置**：`src/main.py` 下载循环
- **根因**：失败只记 debug 日志，文档仍写入 state；下一次增量不会再把已知 URL 当作新文档，文件永久缺失。
- **修复**：新增持久化重试队列 `data/download_retry.json`：
  - 失败文档记录 `attempts`，下一轮自动并入下载列表；
  - 成功即移除；累计 3 次失败后放弃并打 ERROR；
  - 队列写入使用原子写，写失败不影响主流程；
  - 无变更但有重试任务时，不再提前 return。
- **测试**：`DownloadRetryQueueTests`（成功移除、失败累加、3 次放弃、分类范围过滤）。

### 3. Windows 上写数据文件产生全文件 CRLF diff，污染状态提交
- **位置**：`src/storage.py` 的 `atomic_write_json` / `_write_js_data` / `_write_json_data`，以及 crawler 的 txt 写出
- **根因**：`open(..., "w")` / `Path.write_text()` 在 Windows 默认把 `\n` 转成 `\r\n`，导致 `data/*.json`、`JS/*.js` 每次本地运行都整文件变红。
- **修复**：所有文本写出显式 `newline="\n"`；新增 `.gitattributes` 统一 `eol=lf`（含 `data/**`、`JS/**` 与无扩展名配置文件）；把被污染的工作树文件重写回 LF。
- **验证**：`git diff --ignore-cr-at-eol --stat` 与 `git hash-object` 校验，数据/JS 文件已与 HEAD 内容一致。

### 4. 状态文件读取异常时静默回退为空 state，下一次保存会覆盖真实历史
- **位置**：`src/storage.py::Storage.load`
- **根因**：除 `JSONDecodeError` 外的异常（权限、磁盘、编码等）也只记日志并返回空 `StorageState`，随后 `save()` 会把空状态写回。
- **修复**：非 JSON 解析类异常记录 ERROR 后 `raise`，让整轮失败而不是破坏数据；JSON 语法错误仍备份 `.corrupted.<ts>` 后返回空状态（可恢复）。

---

## 二、抓取与附件解析（已修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 5 | 只有 `.pdf` 直链才直接下载；孤立 `.doc/.docx/.xls/.zip` 附件被当成详情页，最终错误保存为 `.txt` | 改用 `is_attachment_url()` 判断，任意附件扩展名都直接下载；仅 PDF 设置 `pdf_url/has_pdf` |
| 6 | `_is_valid_binary` 对未知扩展名一律返回 True，WAF/HTML 错误页会被写成附件 | 增加 ZIP/OLE/RAR/PDF 魔数校验；未知扩展名检测 HTML/XML 头部并拒绝 |
| 7 | `_build_full_url` 手拼路径，`//host/path`、`?query`、`../`、带 query 的详情页都会拼错 | 改为 `urljoin()` + scheme/netloc 校验，拒绝 `javascript:`/`#` |
| 8 | `_find_attachment_link` 取“附件”容器里第一个链接，容易抓到容器内其它 PDF 或“相关链接” | 改为打分排序：同目录 +2、附件/下载文本 +2、文件扩展 +2、PDF +1、`/XXGK/` +1；“相关链接/政策/新闻” −5，阈值兜底 |
| 9 | `_guess_file_extension` 只认 pdf/doc/docx，与附件扩展名正则不一致 | 扩展名集合对齐为 pdf/doc/docx/xls/xlsx/ppt/pptx/wps/ofd/zip/rar |
| 10 | `normalize_date` 只认“YYYY年M月D日”和 ISO，`2024/1/5`、`2024.1.5`、`20240115` 原样返回 | 支持年月日、`-`/`/`/`.` 分隔、8 位紧凑日期，统一输出 ISO |
| 11 | 文本兜底文件在 Windows 上写 CRLF | 显式 `newline="\n"` |

---

## 三、状态、变更检测与 JS 导出（已修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 12 | `_is_repost` 的“标题+日期相同即转载”对全部 22 分类生效，通知公告类同名同日会被误吞 | 标题+日期规则仅限 13/14/15；同文号规则仍适用全部分类 |
| 13 | `sync_js_files` 的 `resolve_pdf_url` 闭包依赖循环内变量，靠晚绑定生效，脆弱 | 改为显式传参 `resolve_pdf_url(doc, existing_pdf_url_by_url)` |
| 14 | 分类空结果保留旧 JS 时，不再同步 `.json`，导致热更新 JSON 与 JS 不一致 | 保留旧 JS 分支同时原子写回 `.json` |
| 15 | `purge_documents` 读 R2 索引无异常保护；对 falsy 值统计不准确 | 捕获读取异常并跳过清理；用 sentinel 判断，falsy 也计数；校验 JSON 形状 |
| 16 | 原子写使用 `mkstemp` 后权限变成 0600 | 落盘前 `chmod 0o644` |
| 17 | 元数据回填只根据“文号新增”触发重下，仅补 `有效性`（尤其变成“失效”）时文件名前缀不会更新 | 文号变化，或影响文件名前缀的 validity 变化，都触发重下；`""` ↔ `有效` 不触发 |
| 18 | R2 索引临时文件用固定 `path.tmp`，并发不安全且写 CRLF | 改为同目录 `mkstemp` + LF + fsync + `os.replace` |

---

## 四、搜索 API（已修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 19 | 栏目代码映射缺失 16/17/18/28/29，这些分类永远抓不到新文档 | 新增 `DWGX→16`、`GATSHHZ→17`、`GJGY→18`、`JYTNDF→28`、`ZFWZNDBB→29`（经 so-gov 检索结果标题核实） |
| 20 | IP 封禁（`code=-101`）被 `except Exception` 吞掉，继续打其它关键词 | 新增 `SearchBannedError`；HTTP 403/429 与 `-101` 向上抛出，整轮中止，避免加深封禁 |
| 21 | `fetch_keyword` / `fetch_keyword_wide` 的赋值语句写在 docstring 前，文档字符串失效 | 调整语句顺序，docstring 生效 |
| 22 | 未使用的 `urlencode` 导入与 `_URL_DATE_RE` 死代码 | 删除 |

---

## 五、通知与 R2（已修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 23 | `EMAIL_SMTP_PORT` 非法值在 `__init__` 抛 `ValueError` 崩进程 | 解析失败 warning 并回退 465 |
| 24 | 邮件附件固定 `_subtype="pdf"`，非 PDF 附件 MIME 类型错误 | 按文件后缀 `mimetypes.guess_type` 推断；非 application 类型用 `MIMEBase` + base64 |
| 25 | `SMTP_SSL` 未设置超时，可能挂死 CI | `timeout=30` |
| 26 | Telegram MarkdownV2 未先转义反斜杠本身 | 先 `\` → `\\`，再转义其它特殊字符 |
| 27 | R2 上传失败且缓存记录没有 `r2_url` 时直接 `KeyError` | 统一先取 `cached_url`，为空则按失败处理，不再下标访问 |

---

## 六、CLI 与运行模式（已修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 28 | `--backfill-r2` 在 R2 未配置时静默下载后跳过上传；`--no-download` 时静默空转 | 两种情况均 ERROR + 退出码 1 |
| 29 | `--perpage` 已无实际作用但仍接受、CI 仍传 200 | 保留兼容但传非默认值时 warning 说明已失效；CI 移除该参数 |
| 30 | 下载失败日志为 debug，线上不可见 | 改为 warning |

---

## 七、CI、脚本与文档（已修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 31 | CI job 无超时，浏览器/SSH 挂死会长期占用 runner | `timeout-minutes: 90` |
| 32 | `setup-uv` 用浮动的 `latest`；`uv sync` 未锁定 | 固定 `0.7.8`；`uv sync --frozen` |
| 33 | state 提交的 `pull --rebase` + `push` 无重试，竞争时整轮失败 | 3 次 rebase/push 重试，失败退出 1 |
| 34 | 清理旧 workflow run 步骤在前序失败时不执行 | 增加 `if: always()` |
| 35 | CI 时间语义不明确（cutoff/通知时间） | job 级 `TZ=Asia/Shanghai` |
| 36 | `data/*.corrupted.*`、原子写临时文件可能被提交 | `.gitignore` 增加对应规则 |
| 37 | `sync_artifact.py` 只比较文件大小，同大小不同内容会漏拷 | 增加分块内容比较 |
| 38 | `verify_downloads.py` 的扩展名检查永远为真（死检查）、orphan 不影响退出码 | 删除死检查；orphan 计入结论与退出码 2；JSON 报告显式 LF |
| 39 | README 仅 2 行、DEVELOPMENT 与实现脱节（仍描述 was5）、AGENTS 模块/Python 版本过时 | 全部重写/更新，覆盖参数、数据契约、重试队列、so-gov 策略、CI、排障 |
| 40 | GitHub Actions 仍用可移动的 tag，存在供应链风险 | 4 个 action 全部 pin 到核实过的 commit SHA（checkout v7.0.1、setup-uv v10.3.3、upload-artifact v7.0.2、delete-workflow-runs v2.1.0）；全部为 Node 24 runtime，消除 Node 20 弃用警告；`validate-so-gov.yml` 同步 pin 与 `--frozen` |
| 41 | 默认路径相对 cwd，离开仓库根运行会把状态写错位置 | `PROJECT_ROOT`/`DATA_DIR`/`JS_DIR`/`DEFAULT_DOWNLOAD_DIR` 锚定仓库根；显式相对 `--download-dir` 仍按 cwd 解析 |
| 42 | 元数据变化的文档完全静默，无法选择接收 | 新增 `--notify-updated`：默认行为不变；开启后 updated 文档并入通知，plain 前缀 `🔄 [更新]`、HTML 增加“更新”徽章 |
| 43 | 17/18/28/29 的历史 state 行按旧 was5 频道错标，新映射无法纠正存量数据 | 新增 `scripts/repair_categories.py`（默认 dry-run，`--apply` 才写回）与 9 个测试；实跑：1942→1686 行，移动 47 行、去重 255 行，17/28/29 清零、18 补足 39 行，9/11/19 分别补入 |
| 44 | `verify_downloads.py --json-out` 仍写 CRLF；`purge_documents` 未校验 R2 索引形状 | JSON 报告改为显式 LF；R2 索引非 dict 时 warning 跳过，避免异常中断 |

---

## 八、验证结果

```bash
uv run python -m py_compile src/*.py scripts/*.py tests/*.py
# OK

PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests -v
# 第一轮 93 tests — OK；接入 was5 与 fork 守卫后 120 tests — OK

uv run python -m src.main --list-categories
# 正常打印 22 个分类，不触网

uv run python scripts/repair_categories.py --categories 17,18,28,29
# dry-run：47 行待移动 + 1 行完全重复；--apply 后
# data/regulations.json: 1942 → 1686 行；17/28/29 = 0，18 = 39，
# 11 = 208、19 = 229；legacy 数组 URL 与 HEAD 一致、last_check 不变、无重复 URL、LF 结尾

git diff --check
# 无空白错误
```

新增测试覆盖：逐分类 baseline、下载重试队列、backfill 守卫、元数据 validity 重同步、非导出分类转载判定、状态读取失败上抛、URL 归一化、二进制魔数校验、附件链接打分、R2 缓存缺失、搜索栏目映射与封禁处理、`--notify-updated` 与通知更新标记、仓库根路径默认值、分类修复脚本的计划/应用/去重。

---

## 九、保留项与残留风险（明确未改）

1. **通知默认仍只发新增文档**：`--notify-updated` 需显式开启，保持既有产品行为；`--download-updated` 与通知开关相互独立。
2. **静态 JSON 发布 `continue-on-error: true`**：发布失败不阻断状态提交，下一轮会全量重发，属最终一致。
3. **R2 只托管 PDF**：doc/docx 等只下载不托管，与既有设计一致。
4. **分类修复只动 state**：`repair_categories.py` 不重命名本地附件、不调整 R2 对象、不触碰 13/14/15 的 JS 导出；无法分类（UNCLASSIFIED）的行保持原分类，可重复执行且幂等。
5. **`data/download_retry.json` 会被 CI 的 `git add data/` 提交**：刻意设计，用于跨日重试；若放弃跨轮持久化需改 `.gitignore`。
6. **本地无法完成真实联网端到端**：本机 `NO_PROXY` 含 `[::1]` 会触发 httpx `InvalidURL`，且 CAAC 有 WAF；联网验证交给 `validate-so-gov.yml` 与每日 CI。
7. **Actions pin 到 SHA 后不会自动跟进上游安全更新**：升级时需重新 `git ls-remote` 并同步注释中的版本号。

---

## 十、变更文件清单

修改（19）：
`.github/workflows/check-updates.yml`、`.github/workflows/validate-so-gov.yml`、`.gitignore`、`AGENTS.md`、`DEVELOPMENT.md`、`README.md`、`data/regulations.json`、`scripts/sync_artifact.py`、`scripts/verify_downloads.py`、`src/crawler.py`、`src/main.py`、`src/notifier.py`、`src/r2_uploader.py`、`src/search_api.py`、`src/storage.py`、`tests/test_crawler.py`、`tests/test_main.py`、`tests/test_storage.py`、`REVIEW_REPORT.md`

新增（6）：
`.gitattributes`、`scripts/repair_categories.py`、`tests/test_notifier.py`、`tests/test_r2_uploader.py`、`tests/test_repair_categories.py`、`tests/test_search_api.py`

后续追加（见第十一~十三节）：新增 `src/was5_api.py`、`tests/test_was5_api.py`、`FORK_SYNC_FOR_AI.md`。

---

## 十一、2026-11-18 追加：心跳通知摘要

**背景**：用户反馈 2026-11-15～11-18 的每日通知都是「0 条」。排查确认并非抓取失效：通知只覆盖 `--notify-categories` 默认的 13/14/15，而这几天民航局没有发布这三类新文件（state 中 13 最新 2026-08-25、14 最新 2026-10-27 发布、15 最新 2026-11-09 发布），实际新增的 9 条都在 9/10/16 类，被通知过滤；`--notify 1` 的每日心跳仍会发一封「0 条」邮件，因此看起来像“没动静”。

**改动**：
- `src/notifier.py`：`format_update_message()` 新增 `other_updates: dict[str, int] | None`；plain 增加一行 `其他分类新增: N 条（分类 A x、分类 B y，未展开）`，HTML 增加对应浅黄色摘要卡。
- `src/main.py`：发送通知前统计 `target_documents` 中不在 `--notify-categories` 内的分类与条数，传给 notifier；`--notify 0` 且通知分类无新增时仍保持静默（不因其他分类变化发信）。
- 测试：`tests/test_notifier.py` 新增 `OtherCategorySummaryTests`（3 例）；`tests/test_main.py` 的假 notifier 与两个心跳/过滤用例补充 `other_updates` 断言。全量 96 tests OK。
- 文档：README / DEVELOPMENT 补充心跳摘要说明。

**效果示例**：
```
📋 CAAC 文件更新通知 (0 条)
检测时间: 2026-11-18 21:30:42
新增文件: 0 条

其他分类新增: 9 条（对外关系 2、政策发布 1、通知公告 6，未展开）
```

**未做**：默认通知分类仍为 13/14/15（产品口径不变）；是否扩大到 9/10/16 或改抓 was5 栏目列表，留待用户决定。

---

## 十二、2026-11-18 追加：was5 栏目列表补充源 + 通知分类扩展

**背景**：上一节排查发现两个结构性缺口——(1) 13/14/15 的召回完全依赖 so-gov 的 32 个关键词搜索，标题里一个关键词都不含的规章会被永久漏掉（例如 `13` 的《基于飞行视角的运输机场运行保障能力反馈工作机制实施办法》不在 was5 第 1 页、也不在关键词命中里，只靠另一路命中）；(2) 默认通知分类只有 13/14/15，而这三类日常更新极少，导致心跳邮件长期是「0 条」。用户确认两个方向都要做。

**新增 `src/was5_api.py`（262 行）**：民航局 was5 栏目列表抓取器，作为 13/14/15 的权威按日期排序补充源。
- `LIST_CHANNELS`：13 用 `channelid=269689`（民航规章专用频道）；14 用 `channelid=211383&fl=14`；15 用 `channelid=211383&fl=15`。**15 没有专用 channelid**（栏目页 `goto()` 只对 `fl=13`/`fl=14`/`fl=guidingdd` 特判），只能走总检索 + `fl` 过滤。
- 行解析：`table.t_table` 每行取第一个 `<a target="_blank" name=标题>` 作标题与 URL；行内 `<li>` 用 `^([^：:]{1,16})[：:]\s*(.*)$` 取 办文单位 / 发文日期 / 文号 / CCAR部号 / 有效性；同页去重。
- `normalize_doc_url()`：**统一成 `http://`**（state 与 so-gov 历史数据都是 http；若归一到 https，每一行都会被判成新文档，产生历史洪水）。
- 分页：`WAS5_MAX_PAGES`（默认 10）页；停止条件为「本页不足 10 行」或「本页所有日期都早于 cutoff」；无日期的行只在前 2 页保留。
- 窗口：`WAS5_DAYS`（默认 `max(--days, 30)`），至少回溯 30 天，防止某轮漏跑把文档永久丢出窗口（重叠部分由 state 去重兜底）。
- HTTP client 懒加载（构造不触网，便于单测）；`_pace()` 0.8–1.5 s 随机间隔。

**`src/crawler.py`**：
- `_fetch_was5_list_hits()` 只抓 `wanted ∩ LIST_CHANNELS`；每个分类独立 try/except，整体再包一层 try/except——**was5 只是补充源，任何失败都不得拖垮整轮**。
- `_merge_search_hits(primary, extra)` 按 URL 并集，so-gov 值优先，was5 只补 so-gov 为空的 title / doc_date / 文号 / 有效性 / 办文单位。
- `_hit_to_document()` 现在把 `doc_number` / `validity` / `office_unit` 带进 `Document`（此前硬编码为空串），因此 was5 行无需抓详情页即可命名与导出。
- `_needs_detail_visit()`：`needs_detail_metadata()` 原语义不变（供 `--repair-metadata` 用），但 enrich 的 pending 判据改为——元数据齐备的 14/15 若只缺成文日期，仅当发布日在 `DETAIL_VISIT_MAX_AGE_DAYS`（30）天内才访问详情页，避免每轮重访无成文日期的老页面。
- 汇总日志区分来源：`(so_gov_hits=..., was5_rows=..., unmapped_url=..., other_category=..., attachments=...)`。

**`src/search_api.py`**：`SearchHit` 新增可选字段 `doc_number` / `validity` / `office_unit`（默认空串，was5 填充，so-gov 保持空）。

**通知口径**：`.github/workflows/check-updates.yml` 的 `NOTIFY_CATEGORIES` 默认值改为 `9,10,13,14,15,16`（通知公告 / 政策发布 / 民航规章 / 规范性文件 / 标准规范 / 对外关系）；README / DEVELOPMENT / AGENTS 同步。

**验证**：
```bash
uv run python -m compileall -q src scripts tests   # OK
uv run python -m unittest discover -s tests        # Ran 120 tests — OK（96 + 24 新增）
```
- was5 全量与 state 对账（window=1000 天）：13 共 15 行、15 行全部同 URL；14 共 100 行、全部同 URL；15 共 100 行、97 行同 URL，3 行是民航局重新发布版（URL 日期 20260930 vs state 20260916），doc_number 相同 → `detect_changes` 判为转载，不会误报新增。
- 端到端 `--days 3 --no-notify --dry-run`（约 3 min，so-gov 96 次请求）：`[was5] merged 7 list rows into search hits`，13/14/15 分别 0/1/6 条，退出码 0。
- 端到端增量 `--no-download --no-notify`（约 4 min）：`so_gov_hits=38, was5_rows=7, unmapped_url=26`；**只检测到 2 条新增（都在 9 通知公告），13/14/15 无洪水**；state 1696 → 1698，证明 was5 新增行被 state 正确去重。

**残留风险**：
- 只有 13/14/15 有 was5 列表源；其余 19 个分类仍只靠关键词搜索召回。
- was5 依赖 `www.caac.gov.cn` 的 was5 服务存活；站点改版或改接口会让补充源静默失效（日志会显示 `was5_rows=0`，不会报错）。
- 15 类没有专用 channelid，只能靠总检索 + `fl=15`，若民航局调整该参数，15 的列表可能变空（同样只在日志里体现）。
- 测试运行会把「state 里已有、旧 JS 里没有」的历史行补进 JS（本次 7 行），`pdf_url` 由 downloads / R2 索引回填（示例行已确认非空）；若某行从未成功下载过，`pdf_url` 仍为空——与旧行为一致，不是新引入的问题。
- JS 行序按「本次抓取顺序 + 保留历史行」拼接，不按日期重排；新增文档会插入数组前部，因此接入 was5 后首次提交会出现约 36 行的纯顺序迁移 diff（内容不变，仅顺序变化），后续轮次趋于稳定。

---

## 十三、2026-11-19 追加：fork 永久分叉的根因修复

**背景**：有用户 fork 了本仓库并**同样启用了每日定时任务**。fork 与上游各自 commit 同一批机器生成的
`data/*.json`、`JS/*`，两条历史相互独立地重写同一批文件，因此 fork 一旦跑过一天就**永久分叉**，
GitHub 网页的 *Sync fork* 只能给出 PR / Discard，无法快进。实测把上游 merge 进该 fork，会在这三个文件上
直接冲突：

```
JS/manifest.json      CONFLICT
data/downloads.json   CONFLICT
data/regulations.json CONFLICT
```

**根因**：`Commit state update` 这一步对任何运行该 workflow 的仓库都生效，fork 也在写 state。

**修复**（`.github/workflows/check-updates.yml`）：给 `Commit state update` 加仓库名守卫——

```yaml
      - name: Commit state update
        if: github.repository == 'wangwingzero/CCAR-workflow'
```

fork 仍然可以跑抓取、上传 artifact、发布静态 JSON（这些步骤各自已有 secret 守卫，缺 secret 就跳过），
但**不再提交 state**，因此永远保持可快进。同步方法也从“merge”改为硬重置：

```bash
gh repo sync <owner>/CCAR-workflow --source wangwingzero/CCAR-workflow --branch main --force
```

**配套文档**：
- `DEVELOPMENT.md` 新增 *Fork Workflow* 章节：双写为何必然分叉、守卫代码、`gh repo sync --force`、
  以及守卫的副作用（硬编码 `owner/repo`，上游改名或转移会导致 state 静默不提交，job 仍绿）。
- 新增 `FORK_SYNC_FOR_AI.md`：给（对方仓库的）AI 执行者的同步执行单。因为 fork 的 state 提交每天都在增加，
  执行单的判定依据是**内容特征**（每条 fork 独有提交都必须是 `chore: update regulation state <日期>`、
  改动文件只有那 3 个自动生成文件），而不是写死的条数；验证步骤包含 compare 必须 `identical`、
  17/28/29 三类脏行必须归零、`GATSHHZ` 栏目映射存在性。

**为什么不能 merge**：fork 跑的是修复前的旧代码，其 state 里保留着 47 条栏目错标行（17=39、18=7、28=239、
29=17），且仍在继续往里写。merge 会把这些脏数据当成本次改动“洗白”进上游，因此只能 discard 后硬重置。

**验证**：
```bash
# 守卫生效：上游自己的 run 仍正常提交 state
gh run view 44347908106 --repo wangwingzero/CCAR-workflow   # success
# → 产生 20c4b32 chore: update regulation state 2026-11-19

uv run python -m unittest discover -s tests                 # Ran 120 tests — OK
```

**残留风险**：
- 守卫是字符串硬编码。上游若重命名或转移到组织名下，`github.repository` 变化会让**上游自己**也停止提交
  state（CI 仍绿，属静默失败）。届时把这一行改成新的 `owner/repo` 即可；排查入口是把该 run 的
  `Commit state update` 步骤日志拉出来看是否 skipped。
- 该守卫只解决“fork 不再分叉”，不解决“fork 的旧数据是脏的”——后者必须靠同步执行单里的硬重置。

## 十四、2026-11-19 追加：定时任务 state 提交失败的根因修复

**现象**：定时 run `44348423270`（2026-11-18T16:32:32Z，schedule，9m39s）**失败**，
而同日手动 run `44347908106`（16:29:54Z，5m30s）成功。失败日志：

```
Auto-merging JS/manifest.json
CONFLICT (content): Merge conflict in JS/manifest.json
Auto-merging data/downloads.json
CONFLICT (content): Merge conflict in data/downloads.json
error: could not apply <sha>... chore: update regulation state 2026-11-19
⚠️  Push attempt 1/3 failed, retrying after rebase
...（同一冲突重复 3 次）...
❌ Could not push state update after 3 attempts
##[error]Process completed with exit code 1
```

**根因**（不是 concurrency 失效）：两个 run 的 job 时间并不重叠——手动 run 于
16:35:23 结束，定时 run 于 16:35:24 才创建、16:35:26 开始，`concurrency`
（`group: caac-check`、`cancel-in-progress: false`）确实把它们**串行化**了。
真正的原因是 `actions/checkout` 默认检出 `github.sha`，而对 `schedule` 事件它是
**触发时刻**的分支头；排队等待（这里约 3 分钟）后，该提交已落后于远端。于是：

1. 定时 run 基于旧 base（`406a095`）跑完抓取，`--days 1` 又不写 state；
2. `Generate data manifest` 重新生成了含新 sha256/totalCount 的 `JS/manifest.json`，
   抓取步骤也写了 `data/downloads.json`；
3. 这两个都是**整文件重写**的机器生成文件，在旧 base 上提交成了“完整快照”；
4. `git pull --rebase` 把该快照 rebase 到已被手动 run 推送的 `20c4b32` 之上 →
   两个文件必然内容冲突；`git rebase --abort` 又回到同一个旧 base，
   3 次重试撞上的是**同一个冲突**，因此全废。

**为什么 fork 守卫没挡住**：守卫只拦 fork，不拦上游自己基于旧 base 提交。

**修复**（`.github/workflows/check-updates.yml`）：

1. 新增步骤 `Fast-forward to the branch tip`（checkout 之后、抓取之前）：
   `git fetch --prune origin main && git reset --hard origin/main`，仅
   `if: github.ref == 'refs/heads/main'`。定时 run 因此总在**当前代码 + 当前 state**
   上运行，不再产出基于旧 base 的快照；顺带修掉“排队期间的代码更新本轮不生效”。
2. 重写 `Commit state update`：不再 rebase，改成**快照替换**——
   - 用 `base_sha` 算出本轮真正改动的路径（`git diff --name-only` + 未跟踪文件）；
   - 每轮尝试先 `mktemp -d` 把这些路径**暂存**，再 `git reset --hard origin/main`，
     然后把暂存文件**复制回工作树**，`git add` 这些路径后提交、`push origin HEAD:main`；
   - 如此永远不发生内容合并 → 不可能冲突；且**只提交本轮写过的文件**，本轮没写的
     文件（如 `--no-download` 时的 `data/downloads.json`）保留远端版本，不会被本地
     过期副本覆盖；远端新增的文件也不会被删。
   - 失败仍在 3 次后退 `exit 1`（不静默）。

**验证**：新增 `scripts/check_state_commit.sh`，用 Ruby 从 workflow 文件里**直接抽取**
这两个 `run:` 块（不是手抄副本，避免与 CI 漂移）并在临时仓库里重放 5 个场景，全部 PASS：

| 场景 | 断言 | 结果 |
|---|---|---|
| 旧 base + 另一 run 已推 state | 退出 0、推送落地、本轮快照胜出、远端新增文件保留、本轮未写文件不动 | PASS |
| 本地无改动 | 退出 0、输出 `No state changes to commit`、不产生提交 | PASS |
| 本地改动与远端一致 | 退出 0、识别 `already matches`、不产生冗余提交 | PASS |
| 推送期间远端又前进一次 | 退出 0、本轮 state 胜出、并发写入保留 | PASS |
| stale 检出的 fast-forward | 工作树落到分支头、新代码可见 | PASS |

命令：`bash scripts/check_state_commit.sh`（需 git + ruby，Windows 用 Git Bash）。

**残留风险**：
- `--days N` 模式本来就不写 state（设计如此），因此定时 run 若被 `DAYS` secret 设成
  非零值，`.gitignore` 之外的 `data/` 变更只来自 manifest/downloads 两处，仍会提交；
  本轮修复保证这类提交不再冲突，但“用 `--days` 跑定时任务”本身会跳过增量的
  state 检测，属既有设计，未改。
- 快照替换语义是“最新一次成功推送的 state 生效”。若某轮抓取不完整（如 so-gov 封禁），
  它仍会用自己的快照覆盖远端——这是既有行为（state 由每轮 crawl 全量重建），
  本轮未改变。真正的护栏是 `SearchBannedError` 会让整轮失败退出，从而不提交。
