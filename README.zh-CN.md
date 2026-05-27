# Civitai Monitor

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-CN.md">中文</a>
</p>

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

监控指定 Civitai 用户的图片和视频更新，自动下载原图/原视频并通过 Telegram Bot 推送到频道。

---

## 功能特性

- 🔍 调用 Civitai 公开 REST API（图片无需 API Key）
- 👥 **多用户监控** — 可同时监控任意数量的创作者
- 🖼 **最高分辨率原图** — 自动将 `width=*` 替换为 `width=original`
- 🎥 **视频下载** — 通过 Backblaze B2 CDN 获取全高清视频（需浏览器 cookies 认证，见下方教程）
- 🤖 **Telegram 管理 Bot** — 通过聊天命令交互管理，支持按钮选择和翻页
- 🔀 **两种扫描模式**：`incremental`（增量，适合定时任务）或 `full`（全量回填）
- 🚦 **NSFW 过滤**：`sfw_only`（仅非敏感）、`nsfw_only`（仅敏感）、`both`（全部）
- 👤 **多用户隔离** — 每个 Telegram 用户有独立的订阅列表和独立的下载进度（`seen_ids_{tg_id}_{username}.json`）
- 🔐 **多号授权** — `authorized_users` 列表支持多个 Telegram ID 管理 Bot
- 🧠 **智能用户名解析** — 支持纯用户名、`@用户名`、Civitai 主页链接（`civitai.com/user/xxx`、`civitai.red/user/xxx`）——非 Civitai 域名一律拒绝
- ✅ **用户存在性验证** — 调 Civitai API 确认用户存在且有公开作品后才添加
- 💾 **去重机制** — 双层保存（每track结束 + main最终保存），防止定时任务竞态导致重复推送
- 📋 **推送审计日志** — 每次推送记录 ID、文件名、下载状态和推送结果
- 🧹 **自动清理** — 自动删除超过 `keep_days` 天的缓存文件
- 🗂 **交互式取消关注和回填** — 按钮选择用户，支持分页

---

## 架构

```
                   ┌──────────────────────┐
                   │   Telegram Bot        │
                   │   (civitai-bot.py)    │
                   │   - /add /remove      │
                   │   - /list /status     │
                   │   - /backfill /scan   │
                   └──────┬───────────────┘
                          │ 命令
                          ▼
  ┌─────────────────────────────────────────────┐
  │  Cron: monitor.py (每10分钟)                  │
  │                                              │
  │  1. 按 Telegram 用户加载订阅列表               │
  │  2. 通过 Civitai API 拉取最新图片              │
  │  3. 对比该用户的 seen_ids 去重                 │
  │  4. 下载新作品（图片 + 视频）                   │
  │  5. 通过 Bot API 推送到 Telegram              │
  │  6. 立即保存该用户进度                         │
  └─────────────────────────────────────────────┘
```

**多用户隔离设计：**
```
config.yaml                       磁盘
──────────────────────────────────────────────
subscriptions:                      seen_ids/
  YOUR_TELEGRAM_USER_ID: [Username] ├ seen_ids_YOUR_TG_ID_Username.json
  ANOTHER_USER_ID: [Username2]      └ seen_ids_ANOTHER_ID_Username2.json
```

---

## 快速开始

### 1. 准备工作

```bash
git clone https://github.com/dorokuma/civitai-monitor.git
cd civitai-monitor
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml — 填入 Bot Token、Chat ID 和订阅列表
```

最小配置示例：

```yaml
telegram:
  bot_token: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
  chat_id: "-1001234567890"

authorized_users:
  - 123456789

subscriptions:
  '123456789':
    - name: "YourTargetUser"
```

### 2b. 命令行参数

```bash
python3 monitor.py                          # 使用 config.yaml（自动查找）
python3 monitor.py --config /path/to.yaml   # 指定自定义配置文件
python3 monitor.py --mode full              # 覆盖运行模式（incremental/full）
python3 monitor.py --user username          # 只处理指定用户名
python3 monitor.py --mode full --user xxx   # 全量回填单个用户
```

`--mode` 和 `--user` 会临时覆盖 config.yaml 中的值，不影响配置文件本身。
管理 Bot 触发回填时就是用 `monitor.py --mode full --user xxx` 这种方式，
不再修改 config.yaml，安全可靠。

### 3. 增量模式运行（默认）

```bash
python3 monitor.py
```

只检查最新图片，后续运行只增量拉取新作品。

### 4. 全量回填（首次拉取全部历史）

```yaml
# 在 config.yaml 中设置：
mode: "full"
```

```bash
python3 monitor.py
```

遍历用户所有历史页面。一次性拉齐全量后，切换回 `incremental` 模式用于定时任务。

### 5. 运行管理 Bot（可选）

```bash
python3 civitai-bot.py
```

或者用 systemd 托管：

```bash
sudo tee /etc/systemd/system/civitai-bot.service <<'SVC'
[Unit]
Description=Civitai Monitor Admin Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/civitai-monitor
ExecStart=/usr/bin/python3 /root/civitai-monitor/civitai-bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC

sudo systemctl daemon-reload
sudo systemctl enable --now civitai-bot
```

---

## 视频下载：Cookies 获取教程

Civitai 的 CDN（`image.civitai.com`）需要登录态（浏览器 cookies）才能返回真正的视频文件。没有 cookies 时，视频下载会失败，只会推送文本链接。

### 从浏览器导出 Cookies

1. **登录** [civitai.com](https://civitai.com)（或 civitai.red）— 需要保持登录状态

2. **安装 Cookies 导出扩展**：
   - Chrome：[Get cookies.txt](https://chrome.google.com/webstore/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid)
   - Firefox：[Get cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt/)

3. **导出**：
   - 打开 civitai.com
   - 点扩展图标 → **Export** → 选择 **Netscape 格式** 保存
   - 得到一个类似 `cookies.txt` 的文件

4. **放到项目目录**（如 `~/civitai-monitor/civitai_cookies.txt`）

5. **在 `config.yaml` 中配置**：
   ```yaml
   http:
     cookies_file: "/path/to/civitai_cookies.txt"
   ```

> **注意：** Cookies 会过期。如果视频下载突然失效，重新导出一份新的 cookies 即可。

### 工作原理

有 cookies 时：
```
image.civitai.com/xxx.mp4  ──301──►  B2 /default（封面图）
                                       │ 替换为 /original
                                       ▼
                                    B2 /original（真视频 ✅）
```

无 cookies 时：
```
image.civitai.com/xxx.mp4  ──301──►  B2 /default（仅封面图 JPEG ❌）
```

---

## 配置说明

完整配置项参见 [`config.yaml.example`](config.yaml.example)。核心配置段：

| 配置段 | 用途 |
|--------|------|
| `mode` | `incremental`（增量，默认）或 `full`（全量回填） |
| `nsfw` | `sfw_only`（仅非敏感）、`nsfw_only`（仅敏感）、`both`（全部） |
| `video_enabled` | 启用/禁用视频检测和下载 |
| `max_video_size_mb` | 跳过大于此值的视频（默认 1024MB） |
| `api` | API 地址、每页数量 |
| `download` | 下载目录、URL 尺寸后缀、缓存保留天数 |
| `http` | User-Agent、Referer、自定义请求头、**cookies_file** |
| `telegram` | **必填** — Bot Token 和频道/群组 Chat ID |
| `authorized_users` | 允许控制 Bot 的 Telegram 用户 ID 列表 |
| `subscriptions` | **按 Telegram 用户隔离**的订阅列表（以 TG 用户 ID 为 key） |
| `data` | 数据目录路径 |

---

## 管理 Bot 命令

| 命令 | 说明 |
|------|------|
| `/add <用户名\|链接\|@名>` | 增加监控对象（自动验证用户是否存在） |
| `/remove` | 交互式按钮列表，支持翻页，点击取消关注 |
| `/list` | 查看当前号的监控列表 |
| `/status` | 查看运行状态（磁盘、已处理数、配置） |
| `/mode <incremental\|full>` | 切换运行模式 |
| `/nsfw <sfw_only\|nsfw_only\|both>` | 切换 NSFW 过滤 |
| `/cleanup [天数]` | 手动清理 N 天前的缓存 |
| `/scan` | 立即执行一次增量扫描 |
| `/backfill` | 交互式按钮列表，选择用户全量回填 |
| `/interval <分钟>` | 设置扫描间隔（默认 10 分钟） |
| `/help` | 显示所有命令说明 |

每个 Telegram 号只能看到和管理**自己**的订阅。

---

## 定时自动化

```bash
# 每 10 分钟执行一次（config.yaml 中需设置 mode: "incremental"）
*/10 * * * * cd ~/civitai-monitor && python3 monitor.py >> monitor.log 2>&1
```

---

## 文件结构

```
civitai-monitor/
├── monitor.py              # 主监控脚本（支持增量 + 全量回填）
├── civitai-bot.py          # 管理 Bot（可选 — 通过 Telegram 管理监控）
├── config.py               # DEPRECATED — 仅作参考
├── Dockerfile              # 容器构建（非 root 用户运行）
├── docker-compose.yml      # Docker 编排
├── .dockerignore           # 构建上下文排除规则
├── config.yaml.example     # 配置模板（全部占位符）
├── requirements.txt        # Python 依赖
├── LICENSE                 # MIT 许可证
├── README.md               # 英文说明
├── README.zh-CN.md         # 中文说明
└── .gitignore

# 运行时生成（不在仓库中）：
# ├── config.yaml           # 你的配置
# ├── seen_ids/             # 各用户的下载进度
# ├── downloads/            # 缓存图片和视频
# ├── civitai_cookies.txt   # 浏览器 cookies（视频认证用）
# └── monitor.log           # 执行日志
```

---

## 免责声明 & 许可证

### 免责声明

> 本软件仅供**学习和个人使用**。
>
> - 本工具下载的所有图片和视频均为其各自创作者的知识产权。
> - 使用者有责任遵守 Civitai 的[服务条款](https://civitai.com/terms-of-service)以及每件作品的单独许可协议。
> - **未经创作者明确许可，请勿重新分发下载的内容。**
> - 作者不对本工具的滥用行为承担任何责任。

### 许可证

[MIT](LICENSE) © dorokuma
