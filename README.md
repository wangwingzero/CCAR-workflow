# CAAC 规章更新监控

> 自动监控中国民航局规章更新，每日邮件通知 + 一键下载

## 功能

- 🕐 每天自动爬取 CAAC 官网规章列表
- 📧 发现更新时发送邮件通知（支持 QQ 邮箱 + 多渠道）
- 📥 自动下载 PDF 并规范化命名
- 📊 历史记录可追溯（Git 版本控制）

## 技术栈

- **爬虫**: Python + Patchright（反检测 Playwright）
- **定时**: GitHub Actions Cron
- **通知**: QQ 邮箱 / PushPlus / Server酱 / Telegram 等
- **存储**: Git Commit（JSON 状态文件）

## 快速开始

### 1. Fork 本仓库

### 2. 配置 Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 | 必填 |
|-------------|------|------|
| `EMAIL_USER` | QQ 邮箱账号 | ✅ |
| `EMAIL_PASS` | QQ 邮箱授权码（16位） | ✅ |
| `EMAIL_TO` | 接收通知的邮箱 | ✅ |
| `PUSHPLUS_TOKEN` | PushPlus Token | 可选 |
| `SC3_PUSH_KEY` | Server酱 Turbo SendKey | 可选 |

### 3. 启用 Actions

首次 fork 后需要手动启用 GitHub Actions。

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
├── .github/
│   └── workflows/
│       └── check-updates.yml    # GitHub Actions 工作流
├── src/
│   ├── __init__.py              # 包初始化
│   ├── crawler.py               # CAAC 爬虫（基于 Patchright 反检测）
│   ├── notifier.py              # 通知管理（Email/PushPlus/Server酱/Telegram）
│   ├── storage.py               # 状态存储和变更检测
│   └── main.py                  # 主入口
├── data/
│   └── regulations.json         # 规章状态数据（自动更新）
├── downloads/                   # PDF 下载目录（.gitignore）
├── pyproject.toml               # 项目依赖（使用 uv 管理）
├── DEVELOPMENT.md               # 开发文档（AI 开发指南）
└── README.md
```

## 手动触发

除了每日自动运行，也可以在 Actions 页面手动触发 workflow。

## 本地测试

```bash
# 进入项目目录
cd CCAR-workflow

# 安装依赖
uv sync

# 安装 Patchright 浏览器
uv run patchright install chromium

# 设置环境变量
export EMAIL_USER="your_qq@qq.com"
export EMAIL_PASS="your_16_char_auth_code"
export EMAIL_TO="receiver@example.com"

# 运行
uv run python -m src.main
```

## 注意事项

1. **反爬限制**: CAAC 官网有安全狗防护，使用 Patchright 反检测
2. **IP 封禁**: 如果 GitHub Actions IP 被封，可能需要配置代理
3. **60 天自动禁用**: 长期无活动的仓库 Actions 会被禁用，需手动重启
4. **QQ 邮箱授权码**: 在 QQ 邮箱设置 → 账户 → POP3/SMTP 服务 → 生成授权码

## License

MIT
