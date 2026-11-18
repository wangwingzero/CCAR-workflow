# Development Guide

## Overview

`CCAR-workflow` monitors the CAAC website for newly published documents. A daily
GitHub Actions job crawls all 22 public-information categories, diffs the result
against `data/regulations.json`, downloads/renames PDFs, mirrors them to
Cloudflare R2, exports mini-program data to `JS/`, publishes JSON to a static
server, notifies subscribers, and commits the new state back to the repo.

## Tech Stack

- Python 3.12+ (CI uses 3.12; `requires-python = ">=3.12"`)
- `uv` for dependency and Python management (`uv.lock` is committed)
- Patchright (anti-detection Playwright) for WAF-protected pages
- `httpx` for the so-gov search API and R2 Worker
- `BeautifulSoup` + `lxml` for detail-page parsing
- `loguru` for logging

## Module Map

| Module | Responsibility |
| --- | --- |
| `src/main.py` | CLI, 7-step pipeline, download-retry queue, notification filtering |
| `src/crawler.py` | so-gov + was5 hits → `Document`, detail metadata, attachment download, WAF detection, filename generation |
| `src/search_api.py` | `api.so-gov.cn/query/s` client, keyword fan-out, column-code → category mapping |
| `src/was5_api.py` | CAAC was5 column lists (13/14/15), row parsing with 文号/有效性/办文单位 |
| `src/storage.py` | state load/save, change detection (new/updated/reposted), JS/JSON atomic export, purge |
| `src/notifier.py` | Email (SMTP_SSL), PushPlus, Telegram (MarkdownV2 with plain fallback) |
| `src/r2_uploader.py` | PDF upload via Worker proxy, public-URL index, legacy host normalization |
| `scripts/` | CI artifact merge, download/index verification, category repair |

## Pipeline (`src/main.py`)

1. **Crawl** — `CaacCrawler.fetch_all_categories()` calls
   `SoGovSearch.fetch_recent()`, classifies hit URLs by `/XXGK/XXGK/<CODE>/`,
   and resolves attachment-only hits back to their detail page.
2. **Detect** — `Storage.detect_changes()` compares by URL:
   - known URL + tracked-field change → `updated`
   - unknown URL + same 文号 (any category), or same title+date for 13/14/15 →
     `reposted` (ignored)
   - otherwise → `new`
   Categories absent from the stored state are recorded as a baseline instead of
   being notified/downloaded in bulk.
3. **Download** — reuses `data/downloads.json` paths, renames to the canonical
   `{validity!}{doc_number}{title}{ext}` form, and falls back to `.txt` text
   extraction only when no attachment can be downloaded.
4. **R2** — uploads 13/14/15 PDFs and records public URLs; missing URLs for
   newly downloaded PDFs abort the run.
5. **Export** — writes `JS/{regulation,normative,specification}.{js,json}` and
   uploads the JSON to R2 (`data/v1/<name>.json`).
6. **Notify** — sends new documents, optionally filtered by
   `--notify-categories`; `--notify-updated` also merges metadata-changed
   documents (marked `🔄 [更新]` in plain text and with a badge in HTML).
   A `--notify 1` heartbeat also lists how many documents were detected in
   other categories (`其他分类新增: N 条`), so a "0 条" email still proves
   the run completed. Attachments are capped at 10.
7. **State** — commits the freshly merged state (skipped in `--days`,
   `--backfill-r2`, and `--dry-run` modes).

All default paths (`data/`, `JS/`, `downloads/`) are anchored to the repository
root via `PROJECT_ROOT` in `src/main.py`, so the CLI can run from any working
directory. An explicitly passed relative `--download-dir` is still resolved
against the current directory.

### Download retry queue

Failed downloads are persisted to `data/download_retry.json` with an attempt
counter. The next incremental run merges those documents back into the download
list; the entry is removed on success and abandoned after
`MAX_DOWNLOAD_ATTEMPTS` (3), with an ERROR log.

## Category Repair

Historical state rows were grouped by the old was5 search channel, so categories
17/18/28/29 contained documents whose URLs belong to other XXGK columns. The
`scripts/repair_categories.py` helper re-classifies rows with
`src.search_api.classify_url()` (URL column code), drops exact duplicate rows,
and keeps rows whose URL cannot be classified. It is a dry-run by default:

```bash
uv run python scripts/repair_categories.py --categories 17,18,28,29
uv run python scripts/repair_categories.py --categories 17,18,28,29 --apply
```

Only `data/regulations.json` is rewritten; R2 objects, local attachments, and JS
exports are untouched (13/14/15 are never re-categorised).

## Search API Notes

- Endpoint: `POST https://api.so-gov.cn/query/s`, `siteCode=bm70000001`,
  `pageSize=20`, `maxPage=10`.
- The site truncates date filtering silently, so `fetch_keyword_wide()` also
  scans without a time filter and applies the cutoff locally.
- `date_from_url()` prefers `tYYYYMMDD` / `P0YYYYMMDD` in the URL over the API
  `docDate`, which is sometimes the 成文日期 and would hide new documents.
- HTTP 403/429 or API `code=-101` raises `SearchBannedError`; the whole run
  aborts instead of hammering a banning endpoint.
- Column codes map to categories, e.g. `MHGZ→13`, `GFXWJ→14`, `BZGF→15`,
  `GJGY→18`, `JYTNDF→28`, `ZFWZNDBB→29`. Unknown codes land in `0`
  (UNCLASSIFIED) and are not exported.

## was5 List Source

so-gov recall depends on the configured keywords: a document whose title holds
none of them is never returned. Categories 13/14/15 therefore also read the
classic CAAC `was5` list service, which serves the complete, date-sorted column
listing:

| Category | URL |
| --- | --- |
| 13 民航规章 | `http://www.caac.gov.cn/was5/web/search?page=N&channelid=269689` |
| 14 规范性文件 | `...?page=N&channelid=211383&fl=14` |
| 15 标准规范 | `...?page=N&channelid=211383&fl=15` |

- Rows carry 文号 (CCAR部号 / 文号) / 有效性 / 办文单位 / 发文日期, so
  `was5_api.parse_list_rows()` fills those fields without a detail-page visit.
- `Was5ListFetcher.fetch_recent()` pages newest-first and stops at the first page
  entirely older than the window (`WAS5_DAYS`, default 7; `WAS5_MAX_PAGES`,
  default 10).
- `CaacCrawler._fetch_was5_list_hits()` merges the rows with the so-gov hits by
  URL; so-gov values win, was5 only fills gaps. A was5 outage is logged and
  tolerated — it never fails the run.
- A complete was5 row still costs one detail visit for 14/15 to read 成文日期
  (`_needs_detail_visit()`), and only while the document is newer than
  `DETAIL_VISIT_MAX_AGE_DAYS` (30) so pages without 成文日期 are not re-read
  forever.

## Data Contracts

`data/regulations.json`:

```json
{
  "documents": {
    "13": [
      {"title": "...", "url": "...", "category_id": "13", "doc_number": "...",
       "validity": "有效", "office_unit": "...", "sign_date": "...",
       "publish_date": "...", "pdf_url": "...", "has_pdf": true}
    ]
  }
}
```

Legacy top-level arrays (`regulations`, `normatives`, `standards`) are still
written for backward compatibility.

`data/downloads.json` maps document URL → `{relative_path, updated_at}`;
`data/r2_uploads.json` maps the same relative path → public URL.

JS export format:

```js
var data = [ ... ];
module.exports = { regulationData: data };
```

The accompanying `.json` file contains the same array for direct HTTP loading;
`JS/manifest.json` carries counts and sha256 hashes.

## File Naming

```
{validity!}{doc_number}{title}{ext}
```

- `validity` is empty/`有效` → no prefix; otherwise `失效!` / `废止!` etc.
- Illegal Windows characters are replaced with `_`; names are capped at 200
  UTF-8 bytes (extension preserved).

## CI

`.github/workflows/check-updates.yml` (daily at 07:25 UTC + manual dispatch):

1. checkout → `uv sync --frozen` → Patchright Chromium
2. run the crawler with `NOTIFY_CATEGORIES=9,10,13,14,15,16`
3. regenerate `JS/manifest.json`
4. publish the four JSON files to the static server over SSH (best effort,
   retried; the next run republishes even without content changes)
5. commit `data/` + `JS/` with rebase/push retries
6. upload `downloads/` as an artifact and prune old workflow runs

`.github/workflows/validate-so-gov.yml` checks `classify_url`, runs a live
search smoke test, compares recall against the committed state, and finishes
with a dry run.

Jobs run with `TZ=Asia/Shanghai`; keep date/cutoff logic in Beijing time.

## Local Development

```bash
uv sync
uv run patchright install chromium
uv run python -m src.main --days 7 --no-notify --dry-run
uv run python -m unittest discover -s tests -v
```

Environment variables and data files are documented in `README.md`.

### Troubleshooting

- **WAF / 502 page** — the crawler retries with exponential backoff and detects
  known WAF markers; keep request pacing at 1.2–2 s and detail-page delays at
  2–5 s.
- **`InvalidURL: Invalid port: ':1]'` locally** — a malformed `NO_PROXY`
  environment variable (e.g. `[::1]`) breaks httpx. Unset/fix `NO_PROXY`, or
  use `httpx.Client(trust_env=False)` in ad-hoc scripts.
- **Chinese console garbling on Windows** — set `PYTHONIOENCODING=utf-8`.
- **Working directory** — default data/JS/download paths are anchored to the
  repository root, so the CLI works from any directory. Only an explicitly
  passed relative `--download-dir` is resolved against the current directory.
