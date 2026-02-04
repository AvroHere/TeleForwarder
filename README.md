# TeleForwarder — Telegram Media Forwarder Bot

A Telegram bot that forwards media from private chats to a target group/channel, with **queueing**, **delay control**, and **caption handling** (including a “clean mode” to remove captions). citeturn0view0

> **Note:** This README was generated automatically (you asked to ignore the existing `README.md`).  
> It’s written to match the repository layout shown on GitHub: `.env.example`, `bot.py`, `requirements.txt`. citeturn0view0

---

## Features

- **Queue system**: sends items one-by-one to reduce flood errors citeturn0view0  
- **Adjustable delay** between sends (default mentioned as 10s) citeturn0view0  
- **Caption tools**
  - Set a custom caption
  - Auto “join link” footer
  - **Clean mode** (remove all captions) citeturn0view0
- **Stats / analytics** (daily + lifetime usage per admin) citeturn0view0
- **Admin-only** (restricted to specific user IDs) citeturn0view0

---

## Project structure

```
.
├─ bot.py
├─ requirements.txt
└─ .env.example
```

---

## Requirements

- Python 3.9+ (recommended)
- A Telegram bot token from **@BotFather** (in Telegram)

---

## Setup

### 1) Clone the repo

```bash
git clone https://github.com/AvroHere/TeleForwarder.git
cd TeleForwarder
```

### 2) Create your `.env`

Copy the example env file and edit it:

```bash
cp .env.example .env
```

> Open `.env` and fill in the required variables (see below).

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Run the bot

```bash
python bot.py
```

---

## Environment variables

This project ships with a `.env.example`. Use it as the source of truth.

Common variables for this kind of bot usually include:

- `BOT_TOKEN` — your bot token from @BotFather  
- `ADMIN_IDS` — one or more Telegram user IDs allowed to use the bot  
- `TARGET_CHAT_ID` — the group/channel ID where messages are forwarded  
- `DEFAULT_DELAY` — delay in seconds (the repo description mentions 10s default) citeturn0view0  
- `JOIN_LINK` — link added as footer (if enabled)

✅ **Important:** Please confirm the exact variable names in `.env.example` and keep them exactly the same in your `.env`.

---

## Usage (typical flow)

1. Start the bot.
2. DM the bot a video/photo/document.
3. The bot queues it and forwards it to the configured target chat.
4. Captions can be kept, replaced, appended with a footer, or removed via “clean mode”. citeturn0view0

---

## Admin commands (examples)

Command names vary by implementation. If your `bot.py` uses different command names, update this section.

Examples you might have:

- `/delay 10` — set delay between sends
- `/clean on|off` — toggle caption removal
- `/caption <text>` — set a default caption
- `/stats` — show daily/lifetime stats

---

## Notes / Tips

- For groups/channels, you typically need the bot added as **admin** (with permission to post).
- If you hit flood limits, increase delay and keep queue enabled.
- Keep `.env` private — never commit it.

---

## License

No license file was shown in the repository listing. Add one if you plan to publish/distribute.
