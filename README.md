# CCAR Workflow

中国民用航空局（CAAC）官网政策文件增量监控与分发工具。

每日定时抓取 CAAC「法定主动公开内容」下的全部 22 个分类（通知公告、政策发布、
民航规章、规范性文件、标准规范等），与本地状态比对，自动完成：

- 新文件通知（邮件 / PushPlus / Telegram，默认只推送 13/14/15 三个专业分类；0 条心跳邮件会附一行“其他分类新增 N 条”摘要）
- PDF 附件下载与 `{文号}{标题}.pdf` 规范化命名
- Cloudflare R2 托管 + 小程序端 `JS/*.js`、`JS/*.json` 数据导出
- 静态服务器数据发布（`data/v1/regulation.json` 等）
- 状态文件提交回仓库，作为下一次增量的基线

数据检索走两条通道取并集：CAAC 站内搜索接口（so-gov，多关键词 + 时间窗）
负责全网召回；13 民航规章 / 14 规范性文件 / 15 标准规范 另有 **was5 栏目列表**
作为权威、按发布日期排序的补充源，列表行本身带文号/有效性/办文单位，可少抓
详情页。详情页解析成文日期、发文日期等剩余元数据；网站有 WAF，抓取使用
Patchright 浏览器。

## 快速开始

```bash
uv sync
uv run patchright install chromium

# 冒烟测试：最近 7 天，不发通知、不写状态
uv run python -m src.main --days 7 --no-notify --dry-run

# 常规增量运行
uv run python -m src.main

# 查看分类 ID
uv run python -m src.main --list-categories
```

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--days N` | 只处理最近 N 天（按 `publish_date`）；不写状态文件 |
| `--categories 9,13,14,15` | 只抓指定分类（默认全部 22 个） |
| `--notify-categories 9,10,13,14,15,16` | 只通知指定分类；抓取/状态/R2/JS 不受影响（CI 默认即这 6 类） |
| `--notify 1` | 即使没有新文件也发送心跳通知（并汇总其他分类新增条数） |
| `--notify-updated` | 元数据变化的文件也并入通知（默认只通知新增文件） |
| `--no-download` / `--no-notify` / `--dry-run` | 跳过下载 / 通知 / 全部写操作 |
| `--repair-txt` | 重新下载历史上只存成 `.txt` 的 PDF |
| `--download-updated` | 手动修复：元数据变化的文件也重新下载 |
| `--backfill-r2` | 补传 R2 缺失的 13/14/15 PDF（需要 R2 配置） |
| `--repair-metadata` | 删除重复附件行，补抓缺失的文号/有效性并重命名 |

> `--perpage` 仍可传但已无意义：搜索接口固定每页 20 条、最多 10 页。
>
> 所有路径都锚定到仓库根目录（`src/main.py` 所在仓库），因此可以在任意工作
> 目录下运行；`--download-dir` 默认是仓库内的 `downloads/`，显式传相对路径时
> 仍按当前工作目录解析。

## 数据文件

| 路径 | 内容 |
| --- | --- |
| `data/regulations.json` | 各分类文档状态（增量比对基线） |
| `data/downloads.json` | 下载索引：URL → 本地相对路径 |
| `data/r2_uploads.json` | R2 索引：本地相对路径 → 公网 URL |
| `data/download_retry.json` | 下载失败重试队列（最多 3 次，成功即移除） |
| `JS/regulation.js` `.json` | 民航规章导出（小程序用） |
| `JS/normative.js` `.json` | 规范性文件导出 |
| `JS/specification.js` `.json` | 标准规范导出 |
| `JS/manifest.json` | 数据清单（数量、sha256、更新时间） |

`downloads/`、`_artifact*/`、`backup_*/` 等运行产物不入库。

## 环境变量

- 通知：`EMAIL_USER`、`EMAIL_PASS`、`EMAIL_TO`、`EMAIL_SENDER`、`EMAIL_SMTP_HOST`、
  `EMAIL_SMTP_PORT`、`PUSHPLUS_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
- R2：`R2_WORKER_URL`、`R2_WORKER_SECRET`、`R2_DOMAIN`（三者齐全才启用）
- 运行：`DAYS`、`NOTIFY_CATEGORIES`、`SO_GOV_DAYS`、`WAS5_DAYS`、`WAS5_MAX_PAGES`、`PLAYWRIGHT_TIMEOUT`
- CI 额外使用 `STATIC_SSH_*`、`STATIC_ROOT` 发布静态 JSON

本地不要把凭据写进仓库；CI 使用 GitHub Secrets。

## 测试

测试使用标准库 `unittest`（无需额外依赖）：

```bash
uv run python -m unittest discover -s tests -v
uv run python -m py_compile src/*.py scripts/*.py
```

辅助脚本：

```bash
uv run python scripts/sync_artifact.py --source _artifact174 --dry-run
uv run python scripts/verify_downloads.py --since 2026-05-29

# 历史状态修复：按 URL 栏目代码重新归类错标行（默认 dry-run，--apply 写回）
uv run python scripts/repair_categories.py --categories 17,18,28,29
uv run python scripts/repair_categories.py --categories 17,18,28,29 --apply
```

`repair_categories.py` 只改 `data/regulations.json`，不触碰 R2 对象、本地附件或
JS 导出；同一 URL 在目标分类已存在时会去重，并在输出里逐行说明。

## 目录结构

```
src/
  main.py          # 流程编排、模式开关、下载重试队列
  crawler.py       # 检索聚合（so-gov + was5）、详情页解析、附件下载、WAF 处理
search_api.py    # so-gov 检索接口客户端
was5_api.py      # was5 栏目列表客户端（13/14/15 补充源）
  search_api.py    # 站内搜索客户端与栏目代码映射
  storage.py       # 状态、变更检测、JS/JSON 原子写出
  notifier.py      # 邮件 / PushPlus / Telegram
  r2_uploader.py   # R2 Worker 上传
scripts/           # 运维辅助脚本
tests/             # unittest 测试
.github/workflows/ # 每日检查、so-gov 校验
```

更多实现细节见 [DEVELOPMENT.md](DEVELOPMENT.md)。
