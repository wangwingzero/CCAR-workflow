# CAAC Regulation Monitor - Development Guide

## Overview

Automated monitoring of CAAC website for regulation updates.

## Tech Stack

- **Platform**: GitHub Actions (daily scheduled runs)
- **Language**: Python 3.12+
- **Package Manager**: uv
- **Browser**: Patchright (anti-detection Playwright)

## Project Structure

```
CCAR-workflow/
├── .github/workflows/check-updates.yml  # GitHub Actions workflow
├── src/
│   ├── crawler.py    # CAAC crawler (Patchright)
│   ├── notifier.py   # Notification manager
│   ├── storage.py    # State storage and change detection
│   └── main.py       # Main entry point
├── data/regulations.json  # Regulation state (auto-updated)
└── pyproject.toml    # Project dependencies
```

## Configuration

### CAAC Website

```python
BASE_URL = "https://www.caac.gov.cn"
WAS5_SEARCH_URL = "https://www.caac.gov.cn/was5/web/search"

# Channel IDs
REGULATION_CHANNEL = "269689"  # Regulations
NORMATIVE_CHANNEL = "238066"   # Normative documents

# Category IDs
REGULATION_FL = "13"
NORMATIVE_FL = "14"
```

### Search URL Format

```
{WAS5_SEARCH_URL}?channelid={CHANNEL}&perpage=100&orderby=-fabuDate&fl={FL}
```

## Local Development

```bash
# Install dependencies
uv sync

# Install browser
uv run patchright install chromium

# Set environment variables
export EMAIL_USER="your@email.com"
export EMAIL_PASS="your_auth_code"

# Run
uv run python -m src.main --days 7

# Dry run (no notifications)
uv run python -m src.main --days 7 --no-notify --dry-run
```

## File Naming Convention

```
{doc_number}{title}.pdf
```

Examples:
- `CCAR-91-R4一般运行和飞行规则.pdf`
- `失效!CCAR-121-R6大型飞机公共航空运输承运人运行合格审定规则.pdf`

## Exit Codes

- 0: Success
- 1: Failure
- 130: User interrupt

## Notes

1. CAAC website has WAF protection - Patchright required
2. Add random delays (2-5s) between requests
3. Use `wait_until="domcontentloaded"` instead of `networkidle`
4. GitHub Actions may be disabled after 60 days of inactivity
