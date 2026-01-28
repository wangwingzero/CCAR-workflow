# CAAC Regulation Monitor

> Automatically monitors CAAC regulation updates with daily email notifications

## Features

- Daily automated crawling of CAAC website
- Email notifications when updates detected
- Automatic PDF download with standardized naming
- Git-based history tracking

## Tech Stack

- **Crawler**: Python + Patchright (anti-detection Playwright)
- **Scheduler**: GitHub Actions Cron
- **Notifications**: Email / PushPlus / Telegram
- **Storage**: Git Commit (JSON state file)

## Quick Start

### 1. Fork this repository

### 2. Configure Secrets

Add in Settings → Secrets and variables → Actions:

| Secret | Description | Required |
|--------|-------------|----------|
| `EMAIL_USER` | Sender email address | ✅ |
| `EMAIL_PASS` | Email authorization code | ✅ |
| `EMAIL_TO` | Recipient email (defaults to EMAIL_USER) | Optional |
| `DAYS` | Send regulations from last N days | Optional |
| `PUSHPLUS_TOKEN` | PushPlus Token | Optional |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | Optional |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | Optional |

### 3. Modify Schedule (Optional)

Default: 15:00 Beijing Time daily. Edit `.github/workflows/check-updates.yml`:

```yaml
# Beijing Time = UTC + 8
# Beijing 8:00  → UTC 0:00  → cron: '0 0 * * *'
# Beijing 15:00 → UTC 7:00  → cron: '0 7 * * *'
# Beijing 20:00 → UTC 12:00 → cron: '0 12 * * *'
- cron: '0 7 * * *'
```

### 4. Enable Actions

Enable GitHub Actions after forking.

## Email Configuration

Supports any SMTP email:

| Provider | SMTP Server | Port |
|----------|-------------|------|
| QQ Mail | smtp.qq.com | 465 |
| 163 Mail | smtp.163.com | 465 |
| Gmail | smtp.gmail.com | 465 |

## File Naming Convention

```
{doc_number}{title}.pdf
```

## Directory Structure

```
CCAR-workflow/
├── .github/workflows/check-updates.yml
├── src/
│   ├── crawler.py    # CAAC crawler
│   ├── notifier.py   # Notification manager
│   ├── storage.py    # State storage
│   └── main.py       # Entry point
├── data/regulations.json
└── pyproject.toml
```

## Local Testing

```bash
cd CCAR-workflow
uv sync
uv run patchright install chromium

export EMAIL_USER="your@email.com"
export EMAIL_PASS="your_auth_code"

# Run (send regulations from last 7 days)
uv run python -m src.main --days 7

# Dry run
uv run python -m src.main --days 7 --no-notify --dry-run
```

## Notes

1. CAAC website has WAF protection - Patchright required
2. Actions may be disabled after 60 days of inactivity
3. Private repos: 2000 minutes/month, ~2-3 minutes per run

## License

MIT
