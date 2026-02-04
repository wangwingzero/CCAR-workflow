# Repository Guidelines

## Project Structure & Module Organization

- `src/` holds the Python package: `main.py` (entry point), `crawler.py`, `notifier.py`, `storage.py`.
- `data/` contains state JSON files (auto-updated by the workflow); treat them as generated artifacts.
- `downloads/` is used for optional PDF downloads (local runs and CI artifacts).
- `.github/workflows/check-updates.yml` defines the scheduled GitHub Actions job.

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

## Coding Style & Naming Conventions

- Python 3.11+; 4-space indentation; keep the existing import ordering.
- `snake_case` for functions/variables, `CapWords` for classes.
- Prefer `loguru` logging over `print`.
- Downloaded file naming follows `{doc_number}{title}.pdf` (e.g., `CCAR-91-R4一般运行和飞行规则.pdf`).
- No formatter/linter is configured; keep changes PEP 8–compliant and minimal.

## Testing Guidelines

- No automated tests or coverage thresholds are currently configured.
- If you add tests, place them under `tests/` and use `test_*.py` naming. Document how to run them (e.g., `uv run pytest`) in the PR.

## Commit & Pull Request Guidelines

- Follow the repo’s conventional style: `type: summary` (examples in history include `feat:` and `chore:`).
- Keep commits scoped; avoid mixing workflow, crawler, and notifier changes.
- PRs should include: a clear description, the exact command(s) run, and any relevant logs/screenshots (e.g., notification output). Link related issues.

## Configuration & Secrets

- Local runs read environment variables such as `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_TO`, `EMAIL_SENDER`, `PUSHPLUS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DAYS`, and `NOTIFY`.
- Use GitHub Actions secrets for CI; never commit credentials or tokens.

## Crawler Operational Notes

- The CAAC site uses WAF protection; Patchright is required.
- Keep request pacing conservative (random 2–5s delays) and use `wait_until="domcontentloaded"` for page loads.
