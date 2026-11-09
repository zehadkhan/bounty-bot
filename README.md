# Bounty Watch

Telegram bot that watches every public bug bounty program (HackerOne, Bugcrowd,
Intigriti, YesWeHack + self-hosted policies from disclose.io) and messages you when:

- 🆕 a new program opens (with payout, scope and an apply checklist)
- 🔭 an existing program adds new in-scope assets
- 📈 a program raises its max payout
- 🚪 a program closes (off by default)

## What you need

1. A Telegram bot token: open Telegram, message **@BotFather**, send `/newbot`, copy the token.
2. A server that stays on (your VPS / Coolify works). Any machine with Docker.
3. That's it. No API keys, no database server — it uses a SQLite file.

## Setup (Docker)

```bash
cp .env.example .env          # paste TELEGRAM_BOT_TOKEN, leave TELEGRAM_CHAT_ID empty
docker compose up -d --build
```

Open your bot in Telegram and send `/start`. It replies with your chat ID.
Put that in `.env` as `TELEGRAM_CHAT_ID`, then:

```bash
docker compose up -d          # restart with the chat ID
```

You'll get a "Bounty Watch is live" message. The first run only records what
exists today; alerts start from the next check, so you won't get 3,000 messages.

## Setup (Coolify)

New resource → Docker Compose (or Dockerfile) from this folder/repo → add the
variables from `.env.example` in the Environment tab → deploy. Keep the `/data`
volume so the bot remembers what it has already seen.

## Without Docker

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs) DB_PATH=./bounty.db
python bot.py
```

## Commands

| Command | What it does |
|---|---|
| `/status` | what's being watched, last check |
| `/check` | check right now |
| `/new 7` | programs first seen in the last 7 days |
| `/top 10` | highest-paying programs |
| `/search shopify` | search name, domain or scope |
| `/p OpenSea` | full details, scope and apply checklist |

The bot only answers your own chat ID.

## Filters (.env)

- `ONLY_PAID=true` – skip programs that don't pay cash
- `MIN_PAYOUT=5000` – skip programs whose published max is below this
  (HackerOne doesn't publish max payout in this data, so those always pass)
- `PLATFORMS=hackerone,bugcrowd` – only these platforms
- `KEYWORDS=api,crypto,fintech` – only alert when one of these appears in the
  name, domain or scope
- `ALERT_SCOPE_CHANGES`, `ALERT_CLOSED` – turn those alert types on/off

## Notes

- Data comes from github.com/arkadiyt/bounty-targets-data (refreshed about
  every 30 min) and github.com/disclose/diodb. Public programs only; private
  invite-only programs aren't visible to anyone outside them.
- Always read the program's policy before testing, and only test in-scope assets.
