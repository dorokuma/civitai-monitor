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
- 🔀 **Two operation modes**: incremental (cron-friendly) or full backfill
- 🚦 **NSFW filter**: sfw-only, nsfw-only, or both
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

### 3. Run (incremental mode — default)

```bash
python3 monitor.py
```

Only the latest images are checked.  Subsequent runs only pick up new uploads.

### 4. Run (full backfill — first-time catch-up)

```yaml
# Set in config.yaml:
mode: "full"
nsfw: "both"
```

```bash
python3 monitor.py
```

Walks every page of the user's gallery.  Use once to catch up, then switch
back to `incremental` mode for cron.

---

## Configuration Reference

See [`config.yaml.example`](config.yaml.example) for the full schema with
descriptive comments.  Key sections:

| Section | Purpose |
|---------|---------|
| `users` | List of Civitai usernames to monitor |
| `mode` | `incremental` (default) or `full` (backfill) |
| `nsfw` | `sfw_only`, `nsfw_only`, or `both` (default) |
| `api` | API base URL, page size |
| `download` | Output directory, URL size-suffix replacements |
| `telegram` | **Required** — bot token and chat/channel ID |
| `data` | Paths for `seen_ids.json` and runtime data |

---

## Mode Comparison

| Feature | `incremental` | `full` |
|---------|---------------|--------|
| Use case | Regular cron job | First-time backfill |
| Pages checked | Latest page(s) only | All pages |
| API calls per poll | 1–2 (depends on nsfw setting) | Many (until exhausted) |
| Duration | Seconds | Minutes to hours |
| Cron-safe | ✅ Yes | ❌ No — one-shot only |

---

## NSFW Behaviour

Civitai's public API does **not** return NSFW images unless the `nsfw=true`
parameter is explicitly passed.  When `nsfw: "both"` (the default), the script
makes **two API calls** per poll: one for SFW and one for NSFW.  NSFW images
can be downloaded from the CDN without authentication.

---

## Automating with Cron

```bash
# Every 10 minutes (requires mode: "incremental" in config.yaml)
*/10 * * * * cd ~/civitai-monitor && python3 monitor.py >> monitor.log 2>&1
```

---

## File Structure

```
civitai-monitor/
├── monitor.py              # Main script (incremental + full backfill)
├── config.yaml.example     # Configuration template (all placeholders)
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT
├── README.md               # English
├── README.zh-CN.md         # 中文
└── .gitignore
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
