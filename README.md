# MUSS Student Union Bot 🏛

An **anonymous** Telegram bot that lets students tell the presidential team what they want
for the school — ideas, events, problems and general messages — and lets the team reply
without ever learning who sent what.

## What students can do

After `/start` the bot shows a menu:

| Button | What it's for |
|---|---|
| 💡 Suggest an idea | Something new to add or change at school |
| 🎉 Event idea | Events, trips, activities |
| ⚠️ Report a problem | Something broken or unfair |
| 💬 Message the team | Questions, feedback, thanks |
| 🗳 Polls | Vote on what the team should do next |
| 🔒 Is it anonymous? | Plain explanation of how anonymity works |

Students can also just type a message and the bot asks what it's about. Every message is
previewed before sending. They get a ticket number, receive the team's replies in the chat,
and can reply back to keep the conversation going — still anonymous. They are notified when
their ticket is marked 👀 Reviewing, ✅ Done or 🙅 Declined.

## What the team (admins) can do

Each submission arrives as `💡 New idea · #12` with the text and the date — nothing else.

- **Reply** to a ticket message → the bot relays it anonymously to the student.
- Buttons under each ticket: 👀 Reviewing / ✅ Done / 🙅 Declined / 🚫 Block sender (spam).
- `/admin` – list of team commands
- `/stats` – numbers at a glance
- `/ticket 12` – full conversation for a ticket
- `/export` – all tickets as a CSV spreadsheet (no identities)
- `/newpoll Which club should we start? | Robotics | Chess | Debate` – sends a poll to everyone
- `/results`, `/closepoll 3` – poll results / close a poll
- `/broadcast text` – announcement to every student
- `/unblock 12` – unblock the sender of ticket #12

Add more team members by listing their Telegram IDs in `ADMIN_IDS` (comma separated). Anyone
can find their ID by messaging [@userinfobot](https://t.me/userinfobot).

## How anonymity is protected

- The bot never stores names, usernames or phone numbers.
- Messages are **copied**, never forwarded, so no account is attached. Only the date is shown.
- Only text and photos are accepted (Telegram strips photo metadata); voice/video/files are refused.
- Poll votes are stored as a keyed hash, so nobody can see who voted for what.
- To deliver replies, the bot privately remembers which chat a ticket came from. That link is
  deleted after `ROUTE_RETENTION_DAYS` (default 60), or immediately when a student sends `/forgetme`.
- Whoever has access to the server/database file can technically see those temporary links,
  so keep the server and `bot.db` private.

## Running it

1. Copy `.env.example` to `.env` and fill in `BOT_TOKEN` and `ADMIN_IDS`.
   **Never commit `.env`** — it's already in `.gitignore`.
2. Run:

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python bot.py
   ```

The bot must be running somewhere 24/7 for students to use it — a cheap VPS, a Raspberry Pi,
[PythonAnywhere](https://www.pythonanywhere.com/) (always-on task), Railway, Render, Fly.io, etc.
With Docker:

```bash
docker build -t muss-bot .
docker run -d --restart unless-stopped --env-file .env -v muss-data:/data muss-bot
```

### Recommended @BotFather setup

- `/setdescription` – "Anonymous line to the MUSS Presidential Team. Share ideas, report problems, vote in polls."
- `/setcommands`:
  ```
  start - Main menu
  polls - Open polls
  privacy - How anonymity works
  help - How to use the bot
  forgetme - Delete the link to your past messages
  cancel - Cancel what you're writing
  ```
