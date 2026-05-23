# Civitai Monitor

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

监控指定 Civitai 用户的图片更新，自动下载原图并通过 Telegram Bot 推送到频道。

---

## 功能特性

- 🔍 调用 Civitai 公开 API（无需 API Key）
- 👥 多用户监控 — 可同时监控任意数量的创作者
- 🖼 自动下载**最高分辨率原图**（自动将 `width=*` 替换为 `width=original`）
- 🤖 通过 Telegram Bot API 直接推送
- 🔀 **两种运行模式**：增量模式（适合定时任务）和全量回填模式
- 🚦 **NSFW 过滤**：仅 SFW、仅 NSFW、或全部拉取
- 💾 去重机制（`seen_ids.json`）— 不会重复处理已见过的图片
- 🛡 优雅的错误处理，自动重试 + 指数退避

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
# 编辑 config.yaml — 填入用户名、Bot Token 和 Chat ID
```

最小配置示例：

```yaml
users:
  - name: "UserOne"

telegram:
  bot_token: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
  chat_id: "-1001234567890"
```

### 3. 增量模式运行（默认）

```bash
python3 monitor.py
```

只检查最新图片，后续运行只增量拉取新作品。

### 4. 全量回填（首次拉取全部历史）

```yaml
# 在 config.yaml 中设置：
mode: "full"
nsfw: "both"
```

```bash
python3 monitor.py
```

遍历用户所有历史页面。一次性拉齐全量后，切换回 `incremental` 模式用于定时任务。

---

## 配置说明

完整配置项参见 [`config.yaml.example`](config.yaml.example)。核心配置段：

| 配置段 | 用途 |
|--------|------|
| `users` | 要监控的 Civitai 用户名列表 |
| `mode` | `incremental`（增量，默认）或 `full`（全量回填） |
| `nsfw` | `sfw_only`（仅非敏感）、`nsfw_only`（仅敏感）、`both`（全部，默认） |
| `api` | API 地址、每页数量 |
| `download` | 下载目录、URL 尺寸后缀、缓存保留天数 |
| `telegram` | **必填** — Bot Token 和频道/群组 Chat ID |
| `data` | `seen_ids.json` 及运行时数据路径 |

---

## 模式对比

| 特性 | `incremental` 增量模式 | `full` 全量模式 |
|------|----------------------|-----------------|
| 用途 | 定时 cron 任务 | 首次回填历史数据 |
| 检查页数 | 仅最新页 | 遍历所有分页 |
| 每次 API 调用 | 1–2 次（取决于 nsfw 设置） | 大量（直到遍历完毕） |
| 耗时 | 数秒 | 数分钟到数小时 |
| 适合定时任务 | ✅ 是 | ❌ 否，一次性使用 |

---

## NSFW 说明

Civitai 公开 API **默认不返回 NSFW 图片**，必须显式传入 `nsfw=true` 参数。
当设置 `nsfw: "both"`（默认值）时，脚本每次会发起**两次 API 调用**：一次拉 SFW、一次拉 NSFW。
NSFW 图片可直接从 CDN 下载，无需登录认证。

---

## 定时自动化

```bash
# 每 10 分钟执行一次（config.yaml 中需设置 mode: "incremental"）
*/10 * * * * cd ~/civitai-monitor && python3 monitor.py >> monitor.log 2>&1
```

---

## 管理 Bot（可选）

运行一个 24/7 的 Telegram Bot，通过聊天命令管理监控：

| 命令 | 说明 |
|------|------|
| `/add <用户名>` | 增加监控对象 |
| `/remove <用户名>` | 移除监控对象 |
| `/list` | 查看监控列表 |
| `/status` | 查看运行状态 |
| `/mode <incremental\|full>` | 切换运行模式 |
| `/nsfw <sfw_only\|nsfw_only\|both>` | 切换 NSFW 过滤 |
| `/cleanup [天数]` | 清理 N 天前的缓存 |
| `/scan` | 立即执行一次增量扫描 |
| `/backfill <用户名>` | 全量回填某个用户 |
| `/help` | 显示所有命令 |

### systemd 部署

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

## 文件结构

```
civitai-monitor/
├── monitor.py              # 主脚本（支持增量 + 全量回填）
├── civitai-bot.py          # 管理 Bot（可选 — 通过 TG 管理监控）
├── config.yaml.example     # 配置模板（全部占位符）
├── requirements.txt        # Python 依赖
├── LICENSE                 # MIT 许可证
├── README.md               # 英文说明
├── README.zh-CN.md         # 中文说明
└── .gitignore
```

---

## 免责声明 & 许可证

### 免责声明

> 本软件仅供**学习和个人使用**。
>
> - 本工具下载的所有图片均为其各自创作者的知识产权。
> - 使用者有责任遵守 Civitai 的[服务条款](https://civitai.com/terms-of-service)以及每张作品的单独许可协议。
> - **未经创作者明确许可，请勿重新分发下载的图片。**
> - 作者不对本工具的滥用行为承担任何责任。

### 许可证

[MIT](LICENSE) © dorokuma
