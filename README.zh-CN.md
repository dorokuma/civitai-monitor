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

### 3. 运行

```bash
python3 monitor.py
```

首次运行会将所有抓取到的图片视为新作品。后续运行只会增量拉取上次轮询之后的新图片。

---

## 配置说明

完整配置项参见 [`config.yaml.example`](config.yaml.example)。核心配置段：

| 配置段 | 用途 |
|--------|------|
| `users` | 要监控的 Civitai 用户名列表 |
| `api` | API 地址、每页数量 |
| `download` | 下载目录、URL 尺寸后缀替换规则 |
| `telegram` | **必填** — Bot Token 和频道/群组 Chat ID |
| `data` | `seen_ids.json` 及运行时数据路径 |

---

## 定时自动化

使用 cron 定时执行脚本，即可实现自动推送通知：

```bash
# 每 10 分钟执行一次
*/10 * * * * cd ~/civitai-monitor && python3 monitor.py >> monitor.log 2>&1
```

也可以使用 systemd timer 或其他定时工具。

---

## 文件结构

```
civitai-monitor/
├── monitor.py              # 主脚本
├── config.yaml.example     # 配置模板（全部占位符）
├── requirements.txt        # Python 依赖
├── LICENSE                 # MIT 许可证
├── README.md               # 英文说明
├── README.zh-CN.md         # 中文说明
├── .gitignore
├── config.yaml             # ⚠ 从模板生成 — 切勿提交
├── seen_ids.json           # 自动生成 — 已处理的图片 ID
└── downloads/              # 自动生成 — 缓存的原图文件
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
