# Family Calendar Bot 🗓️

Telegram bot that converts free-text Hebrew messages into shared Apple Calendar events.

- **NLP:** Gemini 1.5 Flash (cheapest LLM)
- **Calendar:** Apple iCloud via CalDAV
- **Hosting:** Google Cloud Run
- **Storage:** SQLite (ephemeral state + audit log)

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # then fill in real values
python main.py
```

Without `TELEGRAM_WEBHOOK_URL` the bot runs in polling mode — fine for local testing.

## Required setup

1. **Telegram bot** — create via [@BotFather](https://t.me/BotFather), grab token.
2. **iCloud App-Specific Password** — generate at [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific Passwords.
3. **Shared calendar** — create in Apple Calendar, share with both parents (with edit permission), put exact display name in `ICLOUD_CALENDAR_NAME`.
4. **Gemini API key** — free tier at [aistudio.google.com](https://aistudio.google.com).
5. **Telegram user IDs** — message [@userinfobot](https://t.me/userinfobot) from each parent's account.
6. **Group chat ID** — add the bot to a group with both parents, send `/start`, then check `https://api.telegram.org/bot<TOKEN>/getUpdates` for the chat ID (negative number).

## Deploy to Cloud Run

```bash
gcloud run deploy family-calendar-bot \
  --source . \
  --region me-west1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --set-env-vars-file env.yaml
```

After first deploy, set `TELEGRAM_WEBHOOK_URL=https://<service-url>/webhook` and redeploy.

For the 07:00 morning routine on min-instances=0, schedule a Cloud Scheduler job to POST to the webhook URL at 06:59 IDT to wake the container.

## Architecture

See [CLAUDE.md](CLAUDE.md) for the full architecture, state machine, and iCloud quirks.
