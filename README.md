# Civitai Monitor

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Monitor specified Civitai users for new image uploads — automatically download
full-resolution originals and push them to Telegram.

Two operation modes:

| Mode | How it works | Best for |
|------|-------------|----------|
| **Hermes** (default) | Script outputs JSON to stdout; a Hermes Agent cronjob reads it and delivers via `send_message` | Users who run [Hermes Agent](https://hermes-agent.nousresearch.com) |
| **Direct** | Script calls Telegram Bot API directly | Standalone usage without Hermes |

---

## Features

- 🔍 Polls Civitai public API (no API key required)
- 👥 Multi-user monitoring — watch any number of creators
- 🖼 Auto-downloads **full-resolution originals** (replaces `width=*` with `width=original`)
- 📦 **Hermes mode**: JSON output for LLM-driven cronjob agents
- 🤖 **Direct mode**: built-in Telegram Bot push
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
# Edit config.yaml — fill in at least the username(s)
```

Minimal `config.yaml`:

```yaml
users:
  - name: "UserOne"

notifier:
  mode: "hermes"  # or "direct"
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
| `notifier` | Mode selection + Telegram credentials (direct mode) |
| `data` | Paths for `seen_ids.json` and runtime data |

---

## Hermes Cronjob Integration

If you use [Hermes Agent](https://hermes-agent.nousresearch.com/docs), set up
a cronjob that runs every N minutes:

```bash
# Create a cronjob with script mode
hermes cron create \
  --name "civitai-monitor" \
  --schedule "*/10 * * * *" \
  --script "~/civitai-monitor/monitor.py" \
  --deliver "telegram:-1001234567890"
```

The agent reads the script's JSON output and pushes new images as native
Telegram photos with captions.

---

## File Structure

```
civitai-monitor/
├── monitor.py              # Main script
├── config.yaml.example     # Configuration template (all placeholders)
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT
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
