# Civitai Monitor

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-CN.md">中文</a>
</p>

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Monitor specified Civitai users for new image uploads — automatically download
full-resolution originals and push them to a Telegram channel via the Bot API.

---

## Features

- 🔍 Polls Civitai public API (no API key required)
- 👥 Multi-user monitoring — watch any number of creators
- 🖼 Auto-downloads **full-resolution originals** (replaces `width=*` with `width=original`)
- 🤖 Pushes directly to Telegram via Bot API
- 💾 Deduplication via `seen_ids.json` — never re-processes known images
- 🛡 Graceful error handling with retries and back-off

---

## Quick Start

### 1. Prepare

```bash
git clone https://github.com/dorokuma/civitai-monitor.git
cd civitai-monitor
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.yaml.example config.yaml
# Edit config.yaml — fill in the username, bot token, and chat ID
```

Minimal `config.yaml`:

```yaml
users:
  - name: "UserOne"

telegram:
  bot_token: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
  chat_id: "-1001234567890"
```

### 3. Run

```bash
python3 monitor.py
```

On first run all fetched images are treated as new.  Subsequent runs only
pick up images uploaded since the last poll.

---

## Configuration Reference

See [`config.yaml.example`](config.yaml.example) for the full schema with
descriptive comments.  Key sections:

| Section | Purpose |
|---------|---------|
| `users` | List of Civitai usernames to monitor |
| `api` | API base URL, page size |
| `download` | Output directory, URL size-suffix replacements |
| `telegram` | **Required** — bot token and chat/channel ID |
| `data` | Paths for `seen_ids.json` and runtime data |

---

## Automating with Cron

Run the script on a schedule to get automatic push notifications:

```bash
# Every 10 minutes
*/10 * * * * cd ~/civitai-monitor && python3 monitor.py >> monitor.log 2>&1
```

Or use systemd timer — whatever fits your setup.

---

## File Structure

```
civitai-monitor/
├── monitor.py              # Main script
├── config.yaml.example     # Configuration template (all placeholders)
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT
├── README.md               # English
├── README.zh-CN.md         # 中文
├── .gitignore
├── config.yaml             # ⚠ Created from template — never committed
├── seen_ids.json           # Auto-generated — processed image IDs
└── downloads/              # Auto-generated — cached original images
```

---

## Disclaimer & License

### Disclaimer

> This software is provided for **educational and personal use only**.
>
> - All images downloaded by this tool are the intellectual property of
>   their respective creators.
> - Users are responsible for complying with Civitai's
>   [Terms of Service](https://civitai.com/terms-of-service) and the
>   individual licensing terms attached to each artwork.
> - Do **not** redistribute downloaded images without the creator's
>   explicit permission.
> - The authors assume no liability for misuse of this tool.

### License

[MIT](LICENSE) © dorokuma
