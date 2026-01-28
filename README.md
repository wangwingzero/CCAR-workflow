# CAAC 规章更新监控

> 自动监控中国民航局规章更新，每日邮件通知 + 一键下载

## 功能

- 🕐 每天自动爬取 CAAC 官网规章列表
- 📧 发现更新时发送邮件通知（支持任意邮箱 + 多渠道）
- 📥 自动下载 PDF 并规范化命名
- 📊 历史记录可追溯（Git 版本控制）

## 技术栈

- **爬虫**: Python + Patchright（反检测 Playwright）
- **定时**: GitHub Actions Cron
- **通知**: Email / PushPlus / Telegram
- **存储**: Git Commit（JSON 状态文件）

## 快速开始

### 1. Fork 本仓库

### 2. 配置 Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 | 必填 |
|-------------|------|------|
| `EMAIL_USER` | 发件邮箱账号 | ✅ |
| `EMAIL_PASS` | 邮箱授权码/密码 | ✅ |
| `EMAIL_TO` | 收件邮箱（不填则发给自己） | 可选 |
| `DAYS` | 发送最近 N 天的规章（不填则检测新增） | 可选 |
| `PUSHPLUS_TOKEN` | [PushPlus](https://www.pushplus.plus/) Token | 可选 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | 可选 |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | 可选 |

### 3. 修改运行时间（可选）

默认每天北京时间 15:00 运行。如需修改，编辑 `.github/workflows/check-updates.yml` 中的 cron 表达式：

```yaml
# 北京时间 = UTC + 8
# 北京 8:00  → UTC 0:00  → cron: '0 0 * * *'
# 北京 15:00 → UTC 7:00  → cron: '0 7 * * *'
# 北京 20:00 → UTC 12:00 → cron: '0 12 * * *'
- cron: '0 7 * * *'
```

### 4. 启用 Actions

首次 fork 后需要手动启用 GitHub Actions。

## 邮箱配置说明

支持任意 SMTP 邮箱，常见配置：

| 邮箱 | SMTP 服务器 | 端口 | 授权码获取 |
|------|-------------|------|-----------|
| QQ 邮箱 | smtp.qq.com | 465 | [设置 → 账户 → POP3/SMTP](https://mail.qq.com/) |
| 163 邮箱 | smtp.163.com | 465 | [设置 → POP3/SMTP](https://mail.163.com/) |
| Gmail | smtp.gmail.com | 465 | [应用专用密码](https://myaccount.google.com/apppasswords) |

## 文件名规范

下载的 PDF 文件按以下格式命名：

```
{文号}{标题}.pdf
```

示例：
```
CCAR-91-R4一般运行和飞行规则.pdf
AC-91-FS-041航空器运行-航空器操作程序.pdf
失效!CCAR-121-R6大型飞机公共航空运输承运人运行合格审定规则.pdf
```

## 目录结构

```
CCAR-workflow/
├── .github/workflows/check-updates.yml  # GitHub Actions 工作流
├── src/
│   ├── crawler.py    # CAAC 爬虫（Patchright 反检测）
│   ├── notifier.py   # 通知管理（Email/PushPlus/Telegram）
│   ├── storage.py    # 状态存储和变更检测
│   └── main.py       # 主入口
├── data/regulations.json  # 规章状态（自动更新）
└── pyproject.toml    # 项目依赖（uv 管理）
```

## 本地测试

```bash
cd CCAR-workflow
uv sync
uv run patchright install chromium

# 设置环境变量
export EMAIL_USER="your@email.com"
export EMAIL_PASS="your_auth_code"

# 运行（发送最近 7 天的规章）
uv run python -m src.main --days 7

# 试运行（不发送通知）
uv run python -m src.main --days 7 --no-notify --dry-run
```

## 注意事项

1. **反爬限制**: CAAC 官网有安全狗防护，使用 Patchright 反检测
2. **60 天自动禁用**: 长期无活动的仓库 Actions 会被禁用，需手动重启
3. **私有仓库额度**: 每月 2000 分钟，每次运行约 2-3 分钟

## License

MIT
