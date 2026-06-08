# Family Calendar Bot — CLAUDE.md

## What this is
Telegram bot that turns free-text Hebrew messages into Apple Calendar events for a two-parent household.
Hebrew/RTL UI throughout. iCloud CalDAV is the source of truth (no local calendar copy).
Deployed on Google Cloud Run; SQLite is used only for ephemeral conversation state and an audit log.

## Stack
- Python 3.12
- `python-telegram-bot` v21+ (async, webhook mode in production / polling locally)
- `caldav` + `icalendar` for iCloud
- `google-generativeai` (Gemini 2.5 Flash — latest stable Flash model, swappable via `GEMINI_MODEL` env var or `config.GEMINI_MODEL`)
- `APScheduler` — imported but no longer used for the morning routine (kept for future in-process jobs). The daily 07:00 trigger now lives in **Cloud Scheduler → Cloud Run Job** (see "Morning routine" below).
- `sqlite3` (stdlib) — no migration framework, schema in `db.SCHEMA`

## File structure (flat, by design)
- `main.py` — bot entry point: handlers, callbacks, scheduler, webhook server
- `config.py` — env-var loading + `PARENT_MAP`, `CHILDREN`, `TIMEZONE` constants
- `db.py` — SQLite schema and all queries (one module, simple functions)
- `nlp.py` — Gemini wrapper, `ParsedEvent` dataclass, Hebrew prompt template
- `calendar_client.py` — CalDAV connect/read/write + Hebrew event formatting helpers
- `Dockerfile` / `.dockerignore` — Cloud Run build
- `.env.example` — template for required env vars
- `requirements.txt` — pinned deps

## Key conventions
- All user-visible strings are Hebrew. Keep it that way.
- Timezone is hardcoded to `Asia/Jerusalem` (`config.TIMEZONE`). Never use naive datetimes.
- Parents: `טל`, `בן`. Children: `נועם`, `עמית`. (See `config.PARENTS` / `config.CHILDREN`.)
- First-person pronouns in messages map to the *sender* — resolved via `config.PARENT_MAP[telegram_id]`.
- Telegram callback data is namespaced by short prefixes: `p:` parent, `c:` confirm, `f:` force, `m:` morning.
- All bot text uses simple emoji prefixes (✅ ❌ ⚠️ 🕒 📅 📌 🌅) — keep style consistent.

## State machine
Stored in `conversation_state` table, keyed by `chat_id`. Cleared after every commit/cancel.
States: `AWAITING_ASSIGN` → `AWAITING_CONFIRM` (or `AWAITING_FORCE` on conflict) → done.
States older than `config.STATE_TIMEOUT_MINUTES` (default 10) are treated as expired.

```
text → parse → [no parent named? → assignment select (4 buttons)] → conflict check → [conflict? force prompt] → confirm → CalDAV write
```

### Assignment selection (4 buttons)
Triggered when the LLM does NOT find a parent name in the message text (`mentioned_parents` empty).
Applies equally to child events and adult/family events.

| Button | Behavior |
|---|---|
| `טל` / `בן` | Append `" - <name>"` to title, write event normally |
| `שניהם` | Leave title clean, write event without name |
| `להחליט מאוחר` | Write event clean. Insert row in `pending_assignment` so the morning trigger asks who's taking it (day-before AND day-of) |

### Pending assignment (decide-later) flow
- Stored in `pending_assignment` table by event UID + event_date.
- Morning routine queries:
  - All unresolved rows for **today** → asks day-of question
  - Rows for **tomorrow** with `asked_day_before = 0` → asks day-before question (sets flag)
- User answers via `pa:<row_id>:<choice>` callback. On `טל`/`בן` choice, the iCloud event title is updated via `CalDAVClient.update_event_title()`. On `שניהם` the row is just marked resolved. On `אחר כך` nothing changes (will re-ask next morning if event is still ahead).

## SQLite schema (in `db.py`)
| Table | Purpose |
|---|---|
| `conversation_state` | Pending multi-step flow per chat. `payload_json` is the serialised `ParsedEvent` (plus `_pending_assignment` flag). |
| `pending_assignment` | "Decide-later" events awaiting parent assignment via the morning trigger. |
| `morning_answers` | Daily logistics answers, append-only. |
| `caldav_write_log` | Audit log of every event written to iCloud. |

Cloud Run note: the container disk is ephemeral. For durable history, mount a volume at `/data`
or migrate `morning_answers` + `caldav_write_log` to Firestore. `conversation_state` loss is harmless.

## LLM prompt
In `nlp.PROMPT_TEMPLATE`. Returns one of two JSON shapes:
1. Full event: `{title, start_time, end_time, is_recurring, recurrence_rule, involves_children, intent}`
2. Clarification: `{"clarification_needed": "<question in Hebrew>"}`

Validation in `parse_event_text`: filter `involves_children` to known names; coerce `intent` to `event|task`;
retry once on JSON decode failure with a correction hint appended.

## CalDAV gotchas (iCloud)
- Use App-Specific Password from appleid.apple.com — NOT the account password.
- Base URL `https://caldav.icloud.com/`; the library follows 307 redirects to a `pXX-caldav.icloud.com` host. Don't hardcode the p-number.
- All-day events MUST use `icalendar.vDate` (DATE value), not datetime — iCloud silently mis-displays otherwise.
- `DTSTAMP` is required.
- `date_search(expand=True)` to see recurring instances; without it you only get the master VEVENT.
- All `caldav` calls are wrapped in `asyncio.to_thread()` — the library is sync.

## Morning routine
Triggers at 07:00 Asia/Jerusalem (DST-aware) via **Cloud Scheduler → Cloud Run Job**.
Sends 4-button question to `TELEGRAM_GROUP_CHAT_ID`. On answer:
1. Save to `morning_answers`.
2. Write all-day "לוגיסטיקה: …" event to iCloud (best-effort, non-fatal).
3. Send today's calendar summary.

### Production architecture (as of June 2026)
- **Cloud Scheduler job** `morning-routine-cron` (region `me-west1`, schedule `0 7 * * *`, timezone `Asia/Jerusalem`) POSTs to the Cloud Run Jobs `:run` API.
- **Cloud Run Job** `morning-routine` (region `me-west1`) runs the same container image as the bot service, but with `command=python args=morning_job.py`.
- Both run as the default Compute SA `217503293554-compute@developer.gserviceaccount.com`, which already has Secret Manager access for the 5 required secrets (`TELEGRAM_TOKEN`, `TELEGRAM_GROUP_CHAT_ID`, `ICLOUD_USER`, `ICLOUD_APP_PASSWORD`, `ICLOUD_CALENDAR_NAME`) — injected via `--set-secrets` at job-create time.
- Why not GitHub Actions cron: free-tier `schedule` triggers are routinely 1-3h late and occasionally skipped entirely. Cloud Scheduler fires within seconds.
- Why not APScheduler in-process: same module already had a `cmd_cron_morning` handler producing duplicates, and Cloud Run cold starts add risk. Single external trigger is the source of truth.

### `morning_job.py` — standalone entry point
- Loads config, builds calendar client + Telegram bot, runs the same routine the manual "בוקר טוב" handler runs.
- Contains a defensive hour-guard (`GITHUB_EVENT_NAME == "schedule"` → only proceed if local IL hour matches `MORNING_HOUR`). Dead code under Cloud Scheduler but harmless and protects against re-enabling a GitHub workflow.

### Manual trigger ("בוקר טוב" in the group)
Defined inline in `handle_message` (main.py). Two-layer dedup added to prevent the historical "sent twice" bug:
1. **Webhook-retry dedup** — bounded `seen_update_ids` set in `application.bot_data` rejects any `update_id` we've already processed (Telegram retries on slow ACK).
2. **Per-chat debounce** — `last_morning_trigger[chat_id]` timestamp; re-triggers within `_MORNING_DEBOUNCE_SECONDS` (120s) are ignored.

The original `cmd_cron_morning` Telegram command handler was **removed** in commit `72fb8d4` — it duplicated the scheduled fire and passed `CRON_SECRET` as a plaintext command arg.

### Operating the cron
```bash
# Test-run the job now
gcloud run jobs execute morning-routine --region=me-west1 --wait

# Force-fire the scheduler now
gcloud scheduler jobs run morning-routine-cron --location=me-west1

# Pause / resume
gcloud scheduler jobs pause morning-routine-cron --location=me-west1
gcloud scheduler jobs resume morning-routine-cron --location=me-west1

# Last run status
gcloud scheduler jobs describe morning-routine-cron --location=me-west1
```

## Running locally
```
python -m venv .venv
.venv\Scripts\activate    # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env      # then fill in values
python main.py            # polling mode if TELEGRAM_WEBHOOK_URL is empty
```

### Gemini Model Selection
Default model: `gemini-2.5-flash` (latest stable Flash). Override via `.env`:
```
GEMINI_MODEL=gemini-2.5-pro  # or any other available model
```
To list available models on your API key, run:
```python
import google.generativeai as genai
genai.configure(api_key='YOUR_KEY')
for model in genai.list_models():
    print(model.name)
```

## Deploying to Cloud Run
```
gcloud run deploy family-calendar-bot \
  --source . \
  --region me-west1 \
  --allow-unauthenticated \
  --set-env-vars TELEGRAM_TOKEN=...,ICLOUD_USER=...,ICLOUD_APP_PASSWORD=...,...
```
Set `TELEGRAM_WEBHOOK_URL` to `https://<service-url>/webhook` and redeploy so the bot registers it on startup.

## Do not touch
- `.env` — never commit. `.gitignore` blocks it.
- iCloud App-Specific Password — rotate via appleid.apple.com if leaked.
- The Cloud Scheduler job and Cloud Run Job were set up via gcloud once — don't manually edit them in the console without updating CLAUDE.md.

## Removed / decommissioned
- `.github/workflows/morning_trigger.yml` (deleted in commit `7561760`) — GitHub Actions cron was unreliable; replaced by Cloud Scheduler.
- GitHub repo secret `GCP_SA_KEY` — no longer referenced; safe to delete from repo settings.
- `cmd_cron_morning` Telegram command handler in `main.py` (removed in commit `72fb8d4`) — duplicated the scheduled fire.

## Cost
- Cloud Run free tier covers expected traffic (~50 msgs/day → ~1500 req/month).
- Gemini 1.5 Flash: ~$0.02/month at this volume.
- iCloud / Telegram: free.
