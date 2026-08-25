# Civitai Monitor

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">Chinese</a>
</p>

[中文文档](README.zh-CN.md)

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Monitor specified Civitai users for new image & video uploads — automatically
download full-resolution originals and push them to a Telegram channel via the Bot API.

---

## Features

- 🔍 Polls Civitai public REST API (no API key required for images)
- 👥 **Multi-user monitoring** — watch any number of creators
- 🖼 **Full-resolution originals** — auto-replaces `width=*` with `width=original`
- 🎥 **Video support** — downloads full-HD videos via Backblaze B2 CDN (requires browser cookies — see guide below)
- 🤖 **Telegram Admin Bot** — full management via chat commands with interactive buttons
- 🔀 **Two scan modes**: `incremental` (cron-friendly) or `full` (backfill)
- 🚦 **NSFW filter**: `sfw_only`, `nsfw_only`, or `both`
- 👤 **Per-user isolation** — each Telegram user has their own subscription list and independent download progress (`seen_ids_{tg_id}_{username}.json`)
- 🔐 **Multi-user auth** — `authorized_users` list supports multiple Telegram IDs
- 🧠 **Smart username parsing** — accepts plain usernames, `@username`, and Civitai profile URLs (`civitai.com/user/xxx`, `civitai.red/user/xxx`) — non-Civitai domains rejected
- ✅ **User existence validation** — calls Civitai API to confirm the user has public works before adding
- 💾 **Deduplication** — dual-layer save (track-end + final) prevents cron race conditions
- 📋 **Push audit log** — every push is logged with ID, file, download status, and push result
- 🧹 **Auto-cleanup** — removes cached files older than `keep_days`
- 🗂 **Interactive remove & backfill** — button-based user selection with pagination

---

## Architecture

```
                   ┌──────────────────────┐
                   │   Telegram Bot        │
                   │   (civitai-bot.py)    │
                   │   - /add /remove      │
                   │   - /list /status     │
                   │   - /backfill /scan   │
                   └──────┬───────────────┘
                          │ commands
                          ▼
  ┌─────────────────────────────────────────────┐
  │  Cron: monitor.py (every 10 min)             │
  │                                              │
  │  1. Load subscriptions per Telegram user     │
  │  2. Fetch latest images via Civitai API      │
  │  3. Compare against per-user seen_ids        │
  │  4. Download new originals (img + video)     │
  │  5. Push to Telegram via Bot API             │
  │  6. Save per-user progress immediately       │
  └─────────────────────────────────────────────┘
```

**Per-user isolation design:**
```
config.yaml                        Disk
──────────────────────────────────────────────
subscriptions:                      seen_ids/
  YOUR_TELEGRAM_USER_ID: [Username] ├ seen_ids_YOUR_TG_ID_Username.json
  ANOTHER_USER_ID: [Username2]      └ seen_ids_ANOTHER_ID_Username2.json
```

**Important operational notes (v1.1.3+):**

- **Multi-user subscribe vs single push target**: each authorized Telegram user can maintain their own *subscription list*, but media is pushed to the **single** `telegram.chat_id` channel/chat configured in yaml — not one private chat per subscriber.
- **Global `/mode` and `/nsfw`**: these settings apply to the whole scanner (all subscribers), not only the admin who ran the command.
- **`seen_ids` vs `pushed_ids`**: dedupe for “already delivered” is based on **`pushed_ids`**. `seen_ids` tracks scan progress / page cursor safety and may diverge after a failed send.
- **Token dual-track**: prefer env `CIVITAI_BOT_TOKEN` (via `load_env.py` and/or systemd `EnvironmentFile`). Do not store a real token in yaml — `write_config` strips `bot_token` on save.

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
# Edit config.yaml — fill in bot token, chat ID, and subscriptions
```

Minimal `config.yaml`:

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

### 2b. CLI Options

```bash
python3 monitor.py                          # uses config.yaml (auto-search)
python3 monitor.py --config /path/to.yaml   # use a custom config file
python3 monitor.py --mode full              # override mode (incremental/full)
python3 monitor.py --user username          # process only one username
python3 monitor.py --mode full --user xxx   # backfill one user's full gallery
```

The `--mode` and `--user` flags override config.yaml values for that run.
This is especially useful when the Admin Bot triggers a backfill — it runs
`monitor.py --mode full --user xxx` without modifying config.yaml.

### 3. Run (incremental mode — default)

```bash
python3 monitor.py
```

Only latest images are checked. Subsequent runs only pick up new uploads.

### 4. Run (full backfill — initial catch-up)

```yaml
# Set in config.yaml:
mode: "full"
```

```bash
python3 monitor.py
```

Walks every page of the user's gallery. Use once to catch up, then switch
back to `incremental` mode for cron.

### 5. Run the Admin Bot (optional — 24/7 management)

```bash
python3 civitai-bot.py
```

Or via systemd:

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

> **Single-instance constraint**: this service relies on local `fcntl` file
> locks (`.monitor.lock`) for mutual exclusion. It **must run on a single
> host as a single instance only**. Multi-host or multi-instance deployment
> invalidates these locks and will cause **duplicate downloads and duplicate
> pushes** to your Telegram channel.

---

## Video Download: Cookies Guide

Civitai's CDN (`image.civitai.com`) requires an authenticated session (browser
cookies) to serve real video files instead of cover images. Without cookies,
video downloads will silently fail and only text links will be pushed.

### Export Cookies from Browser

1. **Log in** to [civitai.com](https://civitai.com) (or civitai.red) in your browser
   — you need an active login session.

2. **Install a cookies export extension**:
   - Chrome: [Get cookies.txt](https://chrome.google.com/webstore/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid)
   - Firefox: [Get cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt/)

3. **Export**:
   - Go to civitai.com
   - Click the extension icon → **Export** → save as **Netscape format**
   - You should get a file named something like `cookies.txt`

4. **Place the file** in your project directory (e.g. `~/civitai-monitor/civitai_cookies.txt`)

5. **Configure** in `config.yaml`:
   ```yaml
   http:
     cookies_file: "/path/to/civitai_cookies.txt"
   ```

> **Note:** Cookies expire after some time. If video downloads stop working,
> re-export a fresh cookies.txt from your browser.

### How It Works

With cookies:
```
image.civitai.com/xxx.mp4  ──301──►  B2 /default (cover)
                                       │ rewrite to /original
                                       ▼
                                    B2 /original (real video ✅)
```

Without cookies:
```
image.civitai.com/xxx.mp4  ──301──►  B2 /default (cover JPEG only ❌)
```

---

## Configuration Reference

See [`config.yaml.example`](config.yaml.example) for the full schema. Key sections:

| Section | Purpose |
|---------|---------|
| `mode` | `incremental` (default) or `full` (backfill) |
| `nsfw` | `sfw_only`, `nsfw_only`, or `both` (default) |
| `video_enabled` | Enable/disable video detection and download |
| `max_video_size_mb` | Skip videos larger than this (default: 1024) |
| `api` | API base URL, page size |
| `download` | Output directory, URL size-suffix replacements, cache retention |
| `http` | User-Agent, Referer, extra headers, **cookies_file** |
| `telegram` | **Required** — bot token and chat/channel ID |
| `authorized_users` | List of Telegram user IDs allowed to control the Bot |
| `subscriptions` | **Per-Telegram-user** watch lists (keyed by Telegram user ID) |
| `data` | Paths for `seen_ids` directory |

---

## Admin Bot Commands

| Command | Description |
|---------|-------------|
| `/add <name\|url\|@name>` | Add a user to your watch list (auto-validates existence) |
| `/remove` | Interactive button list with pagination to remove a user |
| `/list` | Show your watched users |
| `/status` | Show monitor status (disk, seen count, config) |
| `/mode <incremental\|full>` | Switch scan mode |
| `/nsfw <sfw_only\|nsfw_only\|both>` | Switch NSFW filter |
| `/cleanup [days]` | Manually clean cached images older than N days |
| `/scan` | Trigger an immediate incremental scan |
| `/backfill` | Interactive button list to backfill a user's full gallery |
| `/interval <min>` | Set scan interval in minutes (default: 10) |
| `/help` | Show all commands |

Each Telegram user only sees and manages their **own** subscriptions.

---

## Automating

The bot (`civitai-bot.service`) runs scheduled scans automatically using its internal scheduler. See the **Scheduled Scanning** section above for details on configuring the scan interval via the bot.

The old cron method is still technically possible but no longer the recommended way.

---

## File Structure

```
civitai-monitor/
├── monitor.py              # Main monitor script (incremental + full backfill)
├── civitai-bot.py          # Admin Bot (manage via Telegram)
├── backfill-memory-wrapper.py  # Stricter memory limit for backfill process
├── load_env.py             # .env file loader
├── config.yaml.example     # Configuration template (all placeholders)
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT
├── README.md               # English
├── README.zh-CN.md         # Chinese
├── .gitignore
├── tests/                  # Unit tests
└── .github/workflows/      # CI/CD (lint + test)

# Runtime (auto-generated, not in repo):
# ├── config.yaml           # Your configuration
# ├── active_backfills.json # Backfill recovery state
# ├── interval.json         # Scan interval setting
# ├── civitai_cookies.txt   # Browser cookies for video auth
# ├── seen_ids/             # Per-user download progress
# ├── downloads/            # Cached images & videos
# ├── optimization_logs/    # Audit & optimization documents
# └── monitor.log*          # Log files (journald primary)
```

---

## Disclaimer & License

### Disclaimer

> This software is provided for **educational and personal use only**.
>
> - All images and videos downloaded by this tool are the intellectual
>   property of their respective creators.
> - Users are responsible for complying with Civitai's
>   [Terms of Service](https://civitai.com/terms-of-service) and the
>   individual licensing terms attached to each artwork.
> - Do **not** redistribute downloaded content without the creator's
>   explicit permission.
> - The authors assume no liability for misuse of this tool.

### License

[MIT](LICENSE) © dorokuma

---

## Scheduled Scanning

The bot (`civitai-bot.service`) runs `monitor.py --mode incremental` on a configurable schedule as a background task, using `interval.json` to determine the frequency.

### User-configurable interval

Users can change the scan frequency from Telegram using the bot command:

```
/interval 15
```

- **Default**: 10 minutes (600 seconds)
- **Allowed range**: 1 ~ 1440 minutes
- The change takes effect on the next scan cycle.

### Files

| File | Purpose |
|------|---------|
| `interval.json` | Current user interval (`{"seconds": 600}`) |
