# Setup Guide — Credentials & First Run

Follow these steps in order. Each step ends with a value you'll paste into `.env`.

> **Tip:** Open `.env.example` in your editor now, save a copy as `.env`, and fill in each line as you go. `.env` is gitignored — it never gets pushed.

---

## 1. Telegram bot token (BotFather) — `TELEGRAM_TOKEN`

1. Open Telegram, search for **`@BotFather`** (the official one — verified blue check).
2. Send `/start`, then `/newbot`.
3. **Bot name** (display name): e.g. `Family Calendar`.
4. **Bot username**: must end in `bot`. e.g. `your_family_cal_bot`. Must be globally unique — try variations if taken.
5. BotFather replies with a token that looks like: `123456789:AAH...long-string...`.
6. **Copy it → paste into `.env` as** `TELEGRAM_TOKEN=...`.

While you're here, also configure:
- `/setprivacy` → choose your bot → **Disable** (so the bot can read all group messages, not just commands).
- `/setjoingroups` → **Enable** (so you can add the bot to a group).

---

## 2. Telegram user IDs — `TAL_TELEGRAM_ID` & `BEN_TELEGRAM_ID`

Each parent needs to do this from **their own** Telegram account:

1. Search for **`@userinfobot`** (or `@RawDataBot`).
2. Send `/start`. The bot replies with your numeric ID (e.g., `123456789`).
3. Tal sends, gets ID → paste as `TAL_TELEGRAM_ID=...`.
4. Ben sends, gets ID → paste as `BEN_TELEGRAM_ID=...`.

This is what lets the bot resolve "אני אקח את עמית" → which parent.

---

## 3. Telegram group chat ID — `TELEGRAM_GROUP_CHAT_ID`

1. Create a new Telegram group (e.g., "משפחה — לוח שנה").
2. Add **both parents** + your bot (search for the bot's username from step 1).
3. In the group, send any message (e.g., `hello`).
4. In your browser, open this URL — replace `<TOKEN>` with the bot token from step 1:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
5. You'll see JSON. Look for `"chat":{"id":-100123456789, ...}`. The number (with the leading minus sign) is the group chat ID.
6. **Copy → paste as** `TELEGRAM_GROUP_CHAT_ID=-100123456789`.

> **Tip:** If you don't see anything in `getUpdates`, send another message to the group and refresh the URL. Telegram only returns recent updates.

---

## 4. iCloud App-Specific Password — `ICLOUD_APP_PASSWORD`

⚠️ Do **not** use your regular Apple ID password — it won't work and may trigger a security alert.

1. Go to **[appleid.apple.com](https://appleid.apple.com/)** and sign in.
2. Sidebar: **Sign-In and Security** → **App-Specific Passwords** → **Generate an app-specific password**.
3. Label it something like `family-calendar-bot`.
4. Apple shows a password formatted like `xxxx-xxxx-xxxx-xxxx`. **Copy it now — you can't view it again.**
5. **Paste as** `ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`.
6. Also paste your Apple ID email as `ICLOUD_USER=your.id@icloud.com`.

> If "App-Specific Passwords" is missing: enable two-factor authentication first (Apple requires it).

---

## 5. iCloud shared calendar — `ICLOUD_CALENDAR_NAME`

Decide which calendar the bot writes to. **Strongly recommended:** create a new dedicated calendar so the bot can't accidentally touch your personal events.

### On a Mac
1. Open **Calendar.app**.
2. Menu: **File** → **New Calendar** → **iCloud** → name it (e.g., `משפחה`).
3. Right-click the calendar → **Share Calendar…** → invite the other parent's Apple ID with **"Allow editing"** (NOT "Public Calendar").

### On iPhone
1. **Calendar app** → **Calendars** (bottom) → **Add Calendar** → choose iCloud as the account → name it.
2. Tap the **(i)** next to the new calendar → **Add Person** → invite the other parent's email → make sure **"Allow Editing"** is on.

### Then
- The other parent must **accept** the share invite (email or Calendar app notification).
- **Paste the exact display name** as `ICLOUD_CALENDAR_NAME=משפחה` (case-sensitive, no quotes, exact whitespace).

> Both parents will see the calendar in their Apple Calendar with full edit access — that's how shared logistics works.

---

## 6. Gemini API key — `GEMINI_API_KEY`

1. Go to **[aistudio.google.com](https://aistudio.google.com/)** and sign in with any Google account.
2. Top-left: **Get API key** → **Create API key** → choose a Google Cloud project (or "Create project").
3. Copy the key (starts with `AIza...`).
4. **Paste as** `GEMINI_API_KEY=AIza...`.

**Free tier:** 15 requests/min and 1M tokens/day on Gemini 1.5 Flash. For a family bot this is effectively unlimited.

---

## 7. Webhook URL & secret — leave blank for now

For local testing skip these two. You'll fill them in when deploying to Cloud Run:

```
TELEGRAM_WEBHOOK_URL=
TELEGRAM_WEBHOOK_SECRET=
```

When `TELEGRAM_WEBHOOK_URL` is empty the bot runs in **polling mode** — perfect for first runs from your laptop. No public URL needed.

---

## 8. First run (local)

```bash
cd "D:/Projects/Projects/Schedule"
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # PowerShell
pip install -r requirements.txt
python main.py
```

You should see:
```
[INFO] calendar_client: Connected to iCloud calendar 'משפחה'
[INFO] main: Bot initialized. Morning routine at 07:00 Asia/Jerusalem
```

In Telegram, in the family group, send a test message like:

> תור לרופא לעמית ביום רביעי ב-10:00

Expected: bot replies with the parent-assignment buttons in Hebrew.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `RuntimeError: Calendar 'משפחה' not found. Available: [...]` | Check the calendar name matches exactly — Hebrew, spacing, case. The error logs every available calendar; copy from there. |
| `401 Unauthorized` from iCloud | Wrong App-Specific Password, or you used your regular Apple ID password. Regenerate at appleid.apple.com. |
| Bot doesn't respond in group | Privacy mode wasn't disabled — go back to BotFather → `/setprivacy` → Disable, then **kick + re-add the bot** to the group (privacy change requires re-add). |
| `⚠️ המשתמש שלכם לא ממופה להורה` | One of `TAL_TELEGRAM_ID` / `BEN_TELEGRAM_ID` is wrong or the message came from a different account. |
| Gemini returns `429` | Rare on free tier. The bot retries once automatically. |

---

## Quick credential checklist

Tick these off before first run:

- [ ] `TELEGRAM_TOKEN` from BotFather
- [ ] `TELEGRAM_GROUP_CHAT_ID` from `getUpdates`
- [ ] `TAL_TELEGRAM_ID` from `@userinfobot` (Tal's account)
- [ ] `BEN_TELEGRAM_ID` from `@userinfobot` (Ben's account)
- [ ] `ICLOUD_USER` (Apple ID email)
- [ ] `ICLOUD_APP_PASSWORD` (App-Specific Password, NOT account password)
- [ ] `ICLOUD_CALENDAR_NAME` (exact name of the shared calendar; share accepted by other parent)
- [ ] `GEMINI_API_KEY` from aistudio.google.com
- [ ] BotFather: privacy mode **disabled**
- [ ] Bot added to group with both parents

When all 8 are filled in, run `python main.py` and send a test message.
