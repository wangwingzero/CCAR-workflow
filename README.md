# CAAC Regulation Monitor

Automatically monitors CAAC (Civil Aviation Administration of China) regulation updates.

## Features

- Daily automated crawling of CAAC website
- Email notifications when updates are detected
- Automatic PDF download with standardized naming
- Git-based history tracking

## Setup

1. Fork this repository
2. Configure secrets in Settings → Secrets → Actions
3. Enable GitHub Actions

## Required Secrets

| Secret | Description |
|--------|-------------|
| `EMAIL_USER` | Sender email address |
| `EMAIL_PASS` | Email authorization code |
| `EMAIL_TO` | Recipient email (optional, defaults to EMAIL_USER) |

## Optional Secrets

| Secret | Description |
|--------|-------------|
| `DAYS` | Send regulations from last N days |
| `PUSHPLUS_TOKEN` | PushPlus notification token |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |

## License

MIT
