# CAAC 规章更新监控 - 开发指南

> 本文档为 AI 开发助手提供完整的项目背景、技术决策和实现指南
> 
> **重要**: 开发前请仔细阅读本文档，包含大量经过验证的技术细节和避坑指南

---

## 代码审查修复记录 (2025-01-28)

基于 Google AI 搜索结果，对代码进行了以下优化：

### crawler.py
1. **浏览器实例复用** - 避免每次请求都启动新浏览器，改为复用 Browser 实例，只创建新 Context
2. **重试机制** - 添加指数退避重试（3 次），处理网络波动
3. **流式 PDF 下载** - 使用 `httpx.stream()` + `iter_bytes(chunk_size=8192)`，避免大文件内存溢出
4. **精细化超时配置** - `httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)`
5. **CI 超时增加** - `page.goto()` 超时从 30s 增加到 60s，适应 GitHub Actions 2 核 CPU

### storage.py
6. **JSON 损坏备份** - 解析失败时自动备份损坏文件到 `.corrupted.{timestamp}`

### notifier.py
7. **Telegram MarkdownV2 转义** - 正确转义特殊字符 `_*[]()~\`>#+-=|{}.!`

### main.py
8. **退出码规范** - 0=成功, 1=失败, 130=用户中断
9. **异常堆栈日志** - 使用 `traceback.format_exc()` 记录完整堆栈

### workflow YAML
10. **系统依赖安装** - `patchright install --with-deps chromium` 安装 libgbm 等
11. **超时环境变量** - 添加 `PLAYWRIGHT_TIMEOUT: "60000"`

---

## 项目概述

### 目标

自动监控中国民航局（CAAC）官网的规章更新，发现新增或修改的规章时：
1. 自动下载 PDF 文件（规范化命名）
2. 发送邮件/推送通知
3. 记录变更历史

### 运行环境

- **平台**: GitHub Actions（每日定时运行）
- **语言**: Python 3.12+
- **包管理**: uv（不是 pip！）
- **浏览器**: Patchright（反检测 Playwright）

---

## 技术选型决策

### 为什么选 Python 而不是 Rust？

| 维度 | Python + Patchright | Rust |
|------|---------------------|------|
| **反爬能力** | ⭐⭐⭐⭐⭐ Patchright 内核级反检测 | ⭐⭐ 无等效工具 |
| **开发速度** | ⭐⭐⭐⭐⭐ 有现成代码可复用 | ⭐⭐ 需全部重写 |
| **CI 编译** | ⭐⭐⭐⭐⭐ 零编译 | ⭐⭐ 5-10 分钟消耗 Actions 配额 |
| **生态成熟度** | ⭐⭐⭐⭐⭐ BeautifulSoup 等 | ⭐⭐⭐ 可用但不如 Python |

**关键原因**: CAAC 官网有安全狗防护，Patchright 是目前最强的反检测方案，Rust 生态没有对等工具（chromiumoxide 只是 CDP 封装，无反检测能力）。

### 为什么用 Patchright 而不是 Playwright？

| 特性 | Playwright (原生) | Patchright (反检测版) |
|------|-------------------|----------------------|
| 反检测核心 | 依赖插件（如 playwright-stealth） | **内核级修改**，移除 CDP 泄露 |
| 绕过 WAF 能力 | 容易被高级 WAF 识别 | **专为绕过 Cloudflare/安全狗设计** |
| 特征清除 | 需手动修改 JS 环境，不彻底 | 自动清除 `webdriver: true` 及二进制特征 |
| API 兼容性 | 官方支持 | **100% 继承 Playwright API**，零成本迁移 |

### 为什么用 httpx 而不是 requests？

| 特性 | requests | httpx |
|------|----------|-------|
| 异步支持 | ❌ 仅同步 | ✅ 原生 async/await |
| HTTP/2 | ❌ 仅 HTTP/1.1 | ✅ 支持 |
| 类型提示 | ❌ 无 | ✅ 完整类型注解 |
| 连接池 | 基础 | 更高效，适合高并发 |

**本项目选择**: 使用 httpx 进行 PDF 下载等 HTTP 请求，Patchright 用于获取动态渲染页面。

---

## Patchright 使用详解

### 安装

```bash
# 安装 patchright 包
uv add patchright

# 安装修改版浏览器内核（必须！）
uv run patchright install chromium
```

### Sync vs Async API

Patchright 提供两种 API 风格：

| 特性 | 同步 API (Sync) | 异步 API (Async) |
|------|----------------|------------------|
| 导入 | `from patchright.sync_api import sync_playwright` | `from patchright.async_api import async_playwright` |
| 代码风格 | 顺序执行，直观易读 | 使用 `async/await`，基于协程 |
| 并发能力 | 阻塞式，单线程一次只能处理一个任务 | 非阻塞，高效处理大量并发 |
| 适用场景 | 简单脚本、初学者 | 生产级爬虫、集成到异步框架 |

**本项目选择**: 使用 **同步 API**，因为每天只跑一次，不需要高并发。

### 同步 API 代码示例

```python
from patchright.sync_api import sync_playwright

def fetch_page(url: str) -> str:
    """使用 Patchright 获取页面内容"""
    with sync_playwright() as p:
        # 启动浏览器（headless=True 无头模式）
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            # wait_until="networkidle" 等待网络空闲
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.content()
        finally:
            browser.close()
```

### 反检测验证

可以用以下网站测试反检测效果：
- https://bot.sannysoft.com - 检测 WebDriver 特征
- https://nowsecure.nl - Cloudflare 检测
- https://browserleaks.com/javascript - JS 指纹检测

---

## 参考实现

### 1. CAAC 爬虫参考

**文件**: `D:\screenshot\HuGeScreenshot-tauri\python\huge_sidecar\services\regulation_service.py`

关键代码片段：

```python
# CAAC 官网配置
BASE_URL = "https://www.caac.gov.cn"
WAS5_SEARCH_URL = "https://www.caac.gov.cn/was5/web/search"

# 频道 ID
REGULATION_CHANNEL = "269689"  # 民航规章频道
NORMATIVE_CHANNEL = "238066"   # 规范性文件频道

# 分类 ID (fl 参数)
REGULATION_FL = "13"   # 民航规章分类
NORMATIVE_FL = "14"    # 规范性文件分类
```

**搜索 URL 构造**:
```python
# 规章搜索
f"{WAS5_SEARCH_URL}?channelid={REGULATION_CHANNEL}&perpage=100&orderby=-fabuDate&fl={REGULATION_FL}"

# 规范性文件搜索
f"{WAS5_SEARCH_URL}?channelid={NORMATIVE_CHANNEL}&perpage=100&orderby=-fabuDate&fl={NORMATIVE_FL}"
```

**浏览器获取页面**:
```python
def _fetch_with_browser(self, url: str, browser_type: str) -> str:
    if browser_type == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.content()
        finally:
            browser.close()
```

### 2. 通知系统参考

**文件**: `D:\sign-in\utils\notify.py`

支持的通知渠道：
- Email（QQ 邮箱）- **主要使用**
- PushPlus
- Server酱 Turbo (SCT)
- Telegram
- 钉钉、飞书、企业微信
- Bark (iOS)

**QQ 邮箱关键配置**:
```python
# From 字段必须使用这个格式，否则 502 错误
msg["From"] = formataddr((Header(sender_name, "utf-8").encode(), self.email_user))

# SMTP 服务器
smtp_server = "smtp.qq.com"  # 或从邮箱域名推断

# 使用 SSL 端口 465
with smtplib.SMTP_SSL(smtp_server, 465) as server:
    server.login(self.email_user, self.email_pass)
    server.sendmail(...)
```

### 3. GitHub Actions 参考

**文件**: `D:\sign-in\.github\workflows\daily-check-in.yml`

关键配置：
```yaml
runs-on: ubuntu-22.04

steps:
  # 使用 uv 而不是 pip
  - name: Install uv
    uses: astral-sh/setup-uv@v4
    with:
      version: "latest"
  
  - name: Set up Python
    run: uv python install 3.12
  
  - name: Install dependencies
    run: uv sync
  
  # 安装 Patchright 浏览器
  - name: Install Patchright browsers
    run: uv run patchright install chromium
```

---

## BeautifulSoup 解析最佳实践

### 处理中文编码

```python
from bs4 import BeautifulSoup

# ✅ 正确：传入二进制内容，让 BS4 自动检测编码
html_content = page.content()  # Patchright 返回的是字符串
soup = BeautifulSoup(html_content, "lxml")

# 如果是 requests/httpx 获取的响应
response = httpx.get(url)
soup = BeautifulSoup(response.content, "lxml")  # 用 .content 不是 .text

# 如果自动检测失败，手动指定编码
soup = BeautifulSoup(response.content, "lxml", from_encoding="gb18030")
```

### find_all vs select

| 方法 | 语法风格 | 示例 | 适用场景 |
|------|----------|------|----------|
| `find_all()` | Pythonic API | `soup.find_all("a", class_="link")` | 需要正则匹配、自定义函数过滤 |
| `select()` | CSS 选择器 | `soup.select("a.link")` | 复杂层级定位，熟悉 CSS 语法 |

```python
# find_all 示例
rows = soup.find_all("tr")
links = soup.find_all("a", href=True)
cells = row.find_all("td", class_="t_l")

# select 示例（CSS 选择器）
rows = soup.select("table.t_table tbody tr")
links = soup.select("a[href$='.pdf']")  # href 以 .pdf 结尾
```

---

## 文件名规范

### 格式

```
{文号}{标题}.pdf
```

### 示例

```
CCAR-91-R4一般运行和飞行规则.pdf
AC-91-FS-041航空器运行-航空器操作程序.pdf
失效!CCAR-121-R6大型飞机公共航空运输承运人运行合格审定规则.pdf
```

### 生成逻辑

```python
def generate_filename(document: RegulationDocument) -> str:
    def sanitize(text: str) -> str:
        """替换文件名中的非法字符"""
        return re.sub(r'[<>:"/\\|?*]', '_', text)

    parts = []

    # 有效性前缀（失效的加前缀）
    validity = document.validity.strip()
    if validity in ("失效", "废止"):
        parts.append("失效!")

    # 文号
    doc_number = sanitize(document.doc_number.strip())
    if doc_number:
        parts.append(doc_number)

    # 标题
    title = sanitize(document.title.strip())
    parts.append(title)

    filename = "".join(parts) + ".pdf"

    # 限制文件名长度
    if len(filename) > 200:
        filename = filename[:197] + "....pdf"

    return filename
```

---

## 数据模型

### RegulationDocument

```python
@dataclass
class RegulationDocument:
    title: str           # 文档标题
    url: str             # 详情页 URL
    validity: str        # "有效", "失效", "废止"
    doc_number: str      # 文号（如 CCAR-121-R7）
    office_unit: str     # 发布单位
    doc_type: str        # "regulation" 规章, "normative" 规范性文件
    sign_date: str       # 签发日期 (YYYY-MM-DD)
    publish_date: str    # 发布日期 (YYYY-MM-DD)
    pdf_url: str         # PDF 附件链接
```

### 状态存储 (data/regulations.json)

```json
{
  "last_check": "2025-01-28T08:00:00",
  "regulations": [
    {
      "url": "https://www.caac.gov.cn/...",
      "doc_number": "CCAR-91-R4",
      "title": "一般运行和飞行规则",
      "validity": "有效",
      "publish_date": "2024-01-15",
      "sha256": "abc123..."
    }
  ],
  "normatives": [...]
}
```

---

## 核心流程

```
1. 爬取 CAAC 官网规章列表
   ├── 使用 Patchright 绕过安全狗
   ├── 解析规章列表页 HTML
   └── 提取文档元数据

2. 对比历史状态
   ├── 读取 data/regulations.json
   ├── 检测新增文档（URL 不存在）
   └── 检测更新文档（SHA256 变化）

3. 处理变更
   ├── 下载新增/更新的 PDF
   ├── 规范化文件名
   └── 保存到 downloads/

4. 发送通知
   ├── 生成变更摘要
   ├── 发送邮件（QQ 邮箱）
   └── 可选：PushPlus/Telegram 等

5. 更新状态
   ├── 更新 data/regulations.json
   └── Git commit + push
```

---

## 目录结构

```
CCAR-workflow/
├── .github/
│   └── workflows/
│       └── check-updates.yml    # GitHub Actions 工作流
├── src/
│   ├── __init__.py
│   ├── crawler.py               # CAAC 爬虫（Patchright）
│   ├── notifier.py              # 通知管理
│   ├── storage.py               # 状态存储和对比
│   └── main.py                  # 主入口
├── data/
│   └── regulations.json         # 规章状态数据
├── downloads/                   # PDF 下载目录（.gitignore）
├── pyproject.toml               # 项目依赖
├── .gitignore
├── README.md                    # 用户文档
└── DEVELOPMENT.md               # 开发文档（本文件）
```

---

## JSON 文件读写最佳实践

### 防止中文乱码

```python
import json

data = {"title": "一般运行和飞行规则", "doc_number": "CCAR-91-R4"}

# ✅ 正确：ensure_ascii=False + encoding="utf-8"
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

# ❌ 错误：中文会变成 \uXXXX
with open("data.json", "w") as f:
    json.dump(data, f)  # 缺少 ensure_ascii=False
```

### 原子写入（防止数据损坏）

普通的 `open(..., 'w')` 会在写入开始时立即清空原文件。如果程序崩溃，数据会丢失。

```python
import json
import os
import tempfile

def atomic_write_json(file_path: str, data: dict) -> None:
    """原子写入 JSON 文件，防止数据损坏"""
    dir_name = os.path.dirname(file_path) or "."
    
    # 1. 写入临时文件
    fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # 强制同步到硬盘
        
        # 2. 原子替换（要么成功，要么保留原文件）
        os.replace(temp_path, file_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
```

---

## QQ 邮箱发送详解

### 获取授权码（必须！）

1. 登录 QQ 邮箱网页版
2. 点击 **设置** → **账号**
3. 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务**
4. 点击 **开启 POP3/SMTP服务**
5. 根据提示发送短信验证
6. 获得 **16 位字母授权码**（不是 QQ 密码！）

### 发送邮件代码

```python
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr

def send_email(
    email_user: str,      # QQ 邮箱账号
    email_pass: str,      # 16 位授权码（不是密码！）
    email_to: str,        # 收件人
    subject: str,         # 主题
    content: str,         # 内容
    sender_name: str = "CAAC 规章监控"  # 发件人显示名称
) -> None:
    """发送 QQ 邮箱"""
    
    # 创建邮件
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(content, "html", "utf-8"))
    
    # ⚠️ From 字段格式必须正确，否则 502 错误
    msg["From"] = formataddr((Header(sender_name, "utf-8").encode(), email_user))
    msg["To"] = email_to
    msg["Subject"] = Header(subject, "utf-8")
    
    # 使用 SSL 465 端口（不是 587！）
    with smtplib.SMTP_SSL("smtp.qq.com", 465) as server:
        server.login(email_user, email_pass)
        server.sendmail(email_user, [email_to], msg.as_string())
```

### 常见错误

| 错误码 | 原因 | 解决方案 |
|--------|------|----------|
| **535** | 授权码错误或未开启 SMTP | 检查是否使用 16 位授权码，确认 SMTP 已开启 |
| **554** | 邮件被判定为垃圾邮件 | 确保设置了 From/To/Subject，避免敏感词 |
| **502** | From 字段格式错误 | 使用 `formataddr()` 正确格式化 |

---

## GitHub Actions 详解

### Cron 表达式

GitHub Actions 使用 **UTC 时间**，北京时间 = UTC + 8 小时。

```yaml
on:
  schedule:
    # 格式：分 时 日 月 周
    - cron: '0 0 * * *'   # UTC 00:00 = 北京时间 08:00
    - cron: '0 16 * * *'  # UTC 16:00 = 北京时间 00:00（次日）
```

| 目标北京时间 | Cron 表达式 (UTC) |
|-------------|-------------------|
| 每天 00:00 | `0 16 * * *` |
| 每天 08:00 | `0 0 * * *` |
| 每天 12:00 | `0 4 * * *` |
| 每天 20:00 | `0 12 * * *` |

### Schedule 不触发的常见原因

1. **执行延迟**（最常见）：GitHub 不保证准时，可能延迟几分钟到几小时
2. **分支限制**：schedule 必须在默认分支（main/master）中定义
3. **仓库不活跃**：超过 60 天无活动会自动暂停
4. **语法错误**：YAML 格式不正确

### 解决方案

```yaml
on:
  schedule:
    - cron: '0 0 * * *'
  workflow_dispatch:  # 添加手动触发，方便调试
```

---

## Loguru 日志配置

```python
import sys
from loguru import logger

# 移除默认配置
logger.remove()

# 控制台输出（带颜色）
logger.add(
    sys.stdout,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    level="INFO"
)

# 文件输出（可选，GitHub Actions 中通常不需要）
# logger.add("runtime.log", level="DEBUG", rotation="10 MB")
```

---

## 环境变量

### 必需

| 变量 | 说明 |
|------|------|
| `EMAIL_USER` | QQ 邮箱账号 |
| `EMAIL_PASS` | QQ 邮箱授权码（16位，不是登录密码） |
| `EMAIL_TO` | 接收通知的邮箱 |

### 可选

| 变量 | 说明 |
|------|------|
| `EMAIL_SENDER` | 发件人显示名称 |
| `PUSHPLUS_TOKEN` | PushPlus Token |
| `SC3_PUSH_KEY` | Server酱 Turbo SendKey |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID |

---

## 注意事项（避坑指南）

### 反爬相关

1. **必须使用 Patchright** - CAAC 官网有安全狗防护，普通 Playwright 会被拦截
2. **设置随机延迟** - 请求间隔 2-5 秒，避免触发限流
   ```python
   import random
   import time
   time.sleep(random.uniform(2, 5))
   ```
3. **模拟真实 User-Agent** - Patchright 已自动处理，无需额外配置
4. **处理 networkidle** - 使用 `wait_until="networkidle"` 等待页面完全加载

### GitHub Actions 相关

1. **Cron 不准时** - 可能延迟几十分钟，不要依赖精确时间
2. **60 天自动禁用** - 长期无活动会被禁用，需手动重启
3. **IP 可能被封** - GitHub Actions 使用公共 IP 段，如被封需考虑代理
4. **配额限制** - 私有仓库每月 2000 分钟，注意控制运行时长

### QQ 邮箱相关

1. **使用授权码** - 不是登录密码，在 QQ 邮箱设置中生成
2. **From 字段格式** - 必须用 `formataddr()` 格式化，否则 502 错误
3. **使用 SSL 465 端口** - 不是 587，不需要 STARTTLS
4. **避免频繁发送** - 相同内容频繁发送会被判定为垃圾邮件

### JSON 文件相关

1. **ensure_ascii=False** - 否则中文变成 `\uXXXX`
2. **encoding="utf-8"** - 必须显式指定
3. **原子写入** - 使用临时文件 + `os.replace()` 防止数据损坏

### BeautifulSoup 相关

1. **使用 response.content** - 不是 response.text，让 BS4 自动检测编码
2. **指定解析器** - 推荐 `"lxml"`，比 `"html.parser"` 更快更稳定
3. **处理编码失败** - 如果乱码，尝试 `from_encoding="gb18030"`

---

## 开发步骤建议

### 第一步：实现 crawler.py

1. 复用 `regulation_service.py` 的解析逻辑
2. 使用 Patchright 同步 API 获取页面
3. 实现 `CaacCrawler` 类，包含：
   - `fetch_regulations()` - 获取规章列表
   - `fetch_normatives()` - 获取规范性文件列表
   - `download_pdf()` - 下载 PDF 文件

### 第二步：实现 storage.py

1. JSON 文件读写（原子写入）
2. 变更检测逻辑：
   - 新增：URL 不存在于历史记录
   - 更新：URL 存在但内容变化（可选，通过 SHA256）
3. 实现 `Storage` 类，包含：
   - `load()` - 加载历史状态
   - `save()` - 保存状态（原子写入）
   - `detect_changes()` - 检测变更

### 第三步：实现 notifier.py

1. 复用 `sign-in/utils/notify.py` 的邮件发送逻辑
2. 简化为只支持 Email + PushPlus（可选）
3. 实现 `Notifier` 类，包含：
   - `send_email()` - 发送邮件
   - `format_message()` - 格式化变更消息

### 第四步：实现 main.py

1. 串联整个流程
2. 错误处理和日志
3. 主函数逻辑：
   ```python
   def main():
       # 1. 爬取最新规章列表
       # 2. 加载历史状态
       # 3. 检测变更
       # 4. 如有变更：下载 PDF + 发送通知
       # 5. 保存新状态
   ```

### 第五步：本地测试

```bash
# 安装依赖
uv sync

# 安装浏览器
uv run patchright install chromium

# 设置环境变量（测试用）
export EMAIL_USER="your_qq@qq.com"
export EMAIL_PASS="your_16_char_auth_code"
export EMAIL_TO="receiver@example.com"

# 运行
uv run python src/main.py
```

### 第六步：推送到 GitHub

1. 在仓库 Settings → Secrets 中配置环境变量
2. 手动触发 workflow 测试
3. 检查 Actions 日志确认运行正常

---

## 测试命令

```bash
# 安装依赖
uv sync

# 安装浏览器
uv run patchright install chromium

# 运行完整流程
uv run python src/main.py

# 只测试爬虫
uv run python -c "
from src.crawler import CaacCrawler
crawler = CaacCrawler()
docs = crawler.fetch_regulations()
print(f'找到 {len(docs)} 条规章')
for doc in docs[:3]:
    print(f'  - {doc.doc_number} {doc.title}')
"

# 只测试邮件发送
uv run python -c "
import os
from src.notifier import Notifier
notifier = Notifier()
notifier.send_email('测试主题', '测试内容')
"
```

---

## 附录：完整代码结构

```
CCAR-workflow/
├── .github/
│   └── workflows/
│       └── check-updates.yml    # GitHub Actions 工作流
├── src/
│   ├── __init__.py              # 空文件
│   ├── crawler.py               # CAAC 爬虫
│   │   ├── RegulationDocument   # 数据模型
│   │   ├── CaacCrawler          # 爬虫类
│   │   └── generate_filename()  # 文件名生成
│   ├── storage.py               # 状态存储
│   │   ├── Storage              # 存储类
│   │   └── atomic_write_json()  # 原子写入
│   ├── notifier.py              # 通知管理
│   │   └── Notifier             # 通知类
│   └── main.py                  # 主入口
│       └── main()               # 主函数
├── data/
│   └── regulations.json         # 规章状态数据
├── downloads/                   # PDF 下载目录（.gitignore）
├── pyproject.toml               # 项目依赖
├── .gitignore
├── README.md                    # 用户文档
└── DEVELOPMENT.md               # 开发文档（本文件）
```


---

## 附录：CAAC 官网 HTML 解析细节

### 搜索接口参数

```
https://www.caac.gov.cn/was5/web/search?
  channelid=269689     # 频道 ID（规章: 269689, 规范性文件: 238066）
  &perpage=100         # 每页数量
  &orderby=-fabuDate   # 按发布日期降序
  &fl=13               # 分类 ID（规章: 13, 规范性文件: 14）
  &sw=关键词           # 搜索关键词（可选）
  &fwrq1=2024-01-01    # 开始日期（可选）
  &fwrq2=2024-12-31    # 结束日期（可选）
```

### 规章列表页 HTML 结构

```html
<table class="t_table">
  <tbody>
    <tr>
      <td>序号</td>
      <td class="t_l">
        <a href="/XXGK/XXGK/MHGZ/202401/t20240115_xxx.html">规章标题</a>
        <div class="t_l_content">
          <li>办文单位：中国民用航空局</li>
          <li>发文日期：2024年01月15日</li>
          <li>有效性：有效</li>
        </div>
      </td>
      <td>CCAR-91-R4</td>  <!-- 文号 -->
      <td>有效</td>         <!-- 有效性 -->
    </tr>
  </tbody>
</table>
```

### 解析规章列表的关键代码

```python
def parse_regulation_page(html_content: str) -> list[RegulationDocument]:
    """解析规章搜索结果页面"""
    soup = BeautifulSoup(html_content, "lxml")
    documents = []
    
    # 查找表格
    table = soup.find("table", class_="t_table")
    if not table:
        return documents
    
    # 遍历行
    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
    
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        
        # 标题和链接
        title_cell = row.find("td", class_="t_l") or cells[1]
        link = title_cell.find("a", href=True)
        if not link:
            continue
        
        title = link.get_text(strip=True)
        url = urljoin(BASE_URL, link.get("href", ""))
        
        # 文号
        doc_number = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        
        # 有效性
        validity = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        
        # 从详情 div 提取更多信息
        detail_div = title_cell.find("div", class_="t_l_content")
        office_unit = ""
        publish_date = ""
        
        if detail_div:
            for li in detail_div.find_all("li"):
                text = li.get_text(strip=True)
                if "办文单位：" in text:
                    office_unit = text.replace("办文单位：", "").strip()
                elif "发文日期：" in text or "发文日期:" in text:
                    # 解析日期：2024年01月15日 -> 2024-01-15
                    date_text = re.sub(r"发文日期[：:]", "", text).strip()
                    publish_date = normalize_date(date_text)
        
        documents.append(RegulationDocument(
            title=title,
            url=url,
            validity=validity,
            doc_number=doc_number,
            office_unit=office_unit,
            doc_type="regulation",
            publish_date=publish_date,
        ))
    
    return documents


def normalize_date(date_str: str) -> str:
    """标准化日期格式：2024年01月15日 -> 2024-01-15"""
    if not date_str:
        return ""
    
    match = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    
    # 已经是标准格式
    if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        return date_str
    
    return date_str
```

### 从 URL 提取日期

CAAC 官网的 URL 通常包含日期信息：

```
/XXGK/XXGK/MHGZ/202401/t20240115_xxx.html
                       ^^^^^^^^
                       日期部分
```

```python
def extract_date_from_url(url: str) -> str:
    """从 URL 提取日期"""
    match = re.search(r'/t(\d{4})(\d{2})(\d{2})_', url)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    return ""
```

### 详情页 PDF 链接查找

```python
def find_pdf_link(soup: BeautifulSoup, doc_url: str) -> str | None:
    """在详情页查找 PDF 下载链接"""
    
    # 模式1: 查找附件区域
    attachment_texts = soup.find_all(string=re.compile(r'附件[：:]?', re.I))
    for text in attachment_texts:
        parent = text.parent
        if parent:
            container = parent.parent or parent
            links = container.find_all('a', href=re.compile(r'\.pdf$', re.I))
            if links:
                return build_full_url(links[0].get('href'), doc_url)
    
    # 模式2: 直接查找所有 PDF 链接
    links = soup.find_all('a', href=re.compile(r'\.pdf$', re.I))
    if links:
        return build_full_url(links[0].get('href'), doc_url)
    
    return None


def build_full_url(link: str, doc_url: str) -> str:
    """构建完整 URL"""
    if link.startswith('http'):
        return link
    
    if link.startswith('/'):
        # 绝对路径
        from urllib.parse import urlparse
        parsed = urlparse(doc_url)
        return f"{parsed.scheme}://{parsed.netloc}{link}"
    
    # 相对路径
    doc_dir = '/'.join(doc_url.split('/')[:-1])
    
    # 处理 ../ 和 ./
    while link.startswith('../'):
        link = link[3:]
        doc_dir = '/'.join(doc_dir.split('/')[:-1])
    
    if link.startswith('./'):
        link = link[2:]
    
    return f"{doc_dir}/{link}"
```

---

## 附录：邮件通知模板

### HTML 邮件模板示例

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background: #1a73e8; color: white; padding: 20px; border-radius: 8px 8px 0 0; }
        .content { background: #f8f9fa; padding: 20px; border-radius: 0 0 8px 8px; }
        .item { background: white; padding: 15px; margin: 10px 0; border-radius: 8px; border-left: 4px solid #1a73e8; }
        .item-title { font-weight: bold; color: #1a73e8; }
        .item-meta { color: #666; font-size: 14px; margin-top: 5px; }
        .download-btn { display: inline-block; background: #1a73e8; color: white; padding: 8px 16px; border-radius: 4px; text-decoration: none; margin-top: 10px; }
        .footer { text-align: center; color: #999; font-size: 12px; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>📋 CAAC 规章更新通知</h2>
            <p>检测到 {count} 条新规章/规范性文件</p>
        </div>
        <div class="content">
            {items}
        </div>
        <div class="footer">
            <p>此邮件由 CAAC 规章监控系统自动发送</p>
            <p>检测时间：{timestamp}</p>
        </div>
    </div>
</body>
</html>
```

### 单条规章模板

```html
<div class="item">
    <div class="item-title">{doc_number} {title}</div>
    <div class="item-meta">
        📅 发布日期：{publish_date} | 
        🏢 发布单位：{office_unit} | 
        ✅ 状态：{validity}
    </div>
    <a href="{pdf_url}" class="download-btn">📥 下载 PDF</a>
</div>
```
