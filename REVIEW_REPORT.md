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
# Ran 93 tests in 0.456s — OK

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
