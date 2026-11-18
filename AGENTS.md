# Repository Guidelines

## 始终使用中文进行交流
## Project Structure & Module Organization

- `src/` holds the Python package: `main.py` (entry point), `crawler.py`, `search_api.py`, `notifier.py`, `storage.py`, `r2_uploader.py`.
- `data/` contains state JSON files (auto-updated by the workflow); treat them as generated artifacts.
- `JS/` contains mini-program data exports (`*.js` + `*.json`) generated from the state.
- `downloads/` is used for optional PDF downloads (local runs and CI artifacts).
- `scripts/` holds operational helpers (`sync_artifact.py`, `verify_downloads.py`, `repair_categories.py`).
- `tests/` holds `unittest` tests (`test_*.py`).
- `.github/workflows/check-updates.yml` defines the scheduled GitHub Actions job; `validate-so-gov.yml` validates the search-API integration.

## Build, Test, and Development Commands

```bash
uv sync
```
Install Python dependencies.

```bash
uv run patchright install chromium
```
Install the Patchright browser used by the crawler.

```bash
uv run python -m src.main --days 7
```
Run a local check for documents published in the last 7 days.

```bash
uv run python -m src.main --no-notify --dry-run
```
Smoke test without sending notifications or updating state.

```bash
uv run python -m src.main --list-categories
```
List available category IDs.

```bash
uv run python -m unittest discover -s tests -v
```
Run the test suite (standard library `unittest`; `pytest` is not a dependency).

```bash
uv run python scripts/verify_downloads.py --since 2026-05-29
```
Verify `downloads/` files against `data/downloads.json` (use `--downloads _artifact174` for CI artifact).

```bash
uv run python scripts/sync_artifact.py --source _artifact174
```
Merge a downloaded CI artifact into local `downloads/`.

```bash
uv run python scripts/repair_categories.py --categories 17,18,28,29
```
Dry-run re-classification of state rows by URL column code; add `--apply` to write
`data/regulations.json` (never touches JS/R2/local files).

## Coding Style & Naming Conventions

- Python 3.12+; 4-space indentation; keep the existing import ordering.
- `snake_case` for functions/variables, `CapWords` for classes.
- Prefer `loguru` logging over `print`.
- Downloaded file naming follows `{validity!}{doc_number}{title}{ext}` (e.g., `CCAR-91-R4一般运行和飞行规则.pdf`, `失效!CCAR-121-R6…pdf`).
- Keep repository text files LF (`newline="\n"` when writing); `.gitattributes` enforces it.
- No formatter/linter is configured; keep changes PEP 8–compliant and minimal.

## Testing Guidelines

- Tests live under `tests/` and use the standard-library `unittest` runner
  (`uv run python -m unittest discover -s tests -v`); no coverage threshold is configured.
- Keep tests offline and deterministic (mock network/browser layers).

## Commit & Pull Request Guidelines

- Follow the repo’s conventional style: `type: summary` (examples in history include `feat:` and `chore:`).
- Keep commits scoped; avoid mixing workflow, crawler, and notifier changes.
- PRs should include: a clear description, the exact command(s) run, and any relevant logs/screenshots (e.g., notification output). Link related issues.

## Configuration & Secrets

- Local runs read environment variables such as `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_TO`, `EMAIL_SENDER`, `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `PUSHPLUS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DAYS`, `NOTIFY`, `NOTIFY_CATEGORIES`, `SO_GOV_DAYS`, and `R2_WORKER_URL`/`R2_WORKER_SECRET`/`R2_DOMAIN` (R2 is enabled only when all three are set).
- `NOTIFY_CATEGORIES` (or `--notify-categories`) only trims notification content and attachments to the given category IDs; crawling, the state file, R2 uploads and JS/JSON exports always cover every category. The scheduled workflow defaults it to `9,10,13,14,15,16` (通知公告/政策发布/民航规章/规范性文件/标准规范/对外关系).
- Notifications default to new documents only; pass `--notify-updated` to also include metadata-changed documents (marked as updates).
- Use GitHub Actions secrets for CI; never commit credentials or tokens.

## Crawler Operational Notes

- The CAAC site uses WAF protection; Patchright is required.
- Keep request pacing conservative (random 2–5s delays) and use `wait_until="domcontentloaded"` for page loads.
